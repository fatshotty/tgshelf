"""WebDAV adapter — the read-write data-plane for `rclone mount`.

A thin, stateless translator over the `FileSystem` facade, mounted under `/dav`
on the same aiohttp app (so it reuses the existing Basic-auth + CIDR middleware).
rclone speaks to it with `type=webdav, vendor=other`; every verb maps 1:1 onto a
facade call:

  PROPFIND → resolve + children     GET/HEAD → stream_node (shared with /download)
  PUT      → fs.write (streaming)    MKCOL    → fs.mkdir
  DELETE   → fs.delete (soft)        MOVE     → fs.move / fs.rename
  COPY     → fs.copy                 OPTIONS  → DAV: 1 advertisement

No LOCK/UNLOCK (rclone `vendor=other` does not need class-2 locking). Auto-refresh
of the rclone cache is NOT done here — it is the changes-feed → `vfs/forget`
bridge (control-plane). This module only serves bytes and the tree.

WebDAV status codes differ from the JSON API's (e.g. an occupied MOVE/COPY
destination with `Overwrite: F` → 412, MKCOL over an existing node → 405): they
are produced here, not via the JSON error middleware.
"""

from __future__ import annotations

import logging
from datetime import timezone
from email.utils import format_datetime
from typing import AsyncIterator, Callable
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

from aiohttp import web

from tgshelf.core.fs import FileSystem, InvalidMove, NotAFolder, NotAReadableFile
from tgshelf.db.models import Node
from tgshelf.db.repo import DuplicateNameError
from tgshelf.http.api import open_fs
from tgshelf.http.app import RUNTIME
from tgshelf.http.download import stream_node
from tgshelf.http.rcregistry import RcRegistry, is_rc_authorized
from tgshelf.http.upload import safe_filename

log = logging.getLogger("tgshelf.http.webdav")

DAV_PREFIX = "/dav"
_READ_CHUNK = 64 * 1024
_ALLOW = "OPTIONS, PROPFIND, GET, HEAD, PUT, MKCOL, DELETE, MOVE, COPY"


# -- path helpers -----------------------------------------------------------


def _normalize(path: str) -> str:
    """A decoded WebDAV path → a canonical drive path: leading '/', no trailing
    slash (except root '/'). The `/dav` prefix is stripped if present."""
    if path.startswith(DAV_PREFIX):
        path = path[len(DAV_PREFIX) :]
    if not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/")
    return path or "/"


def _drive_path(request: web.Request) -> str:
    """Drive path of the request target (the route captures everything after
    `/dav`)."""
    return _normalize("/" + request.match_info.get("path", ""))


def _split_parent(drive_path: str) -> tuple[str, str]:
    """('/a/b/c') → ('/a/b', 'c'); ('/c') → ('/', 'c')."""
    parent, _, name = drive_path.rstrip("/").rpartition("/")
    return (parent or "/"), name


def _child_path(parent: str, name: str) -> str:
    """Drive path of a child given its parent's drive path."""
    return f"/{name}" if parent == "/" else f"{parent}/{name}"


def _href(drive_path: str, is_folder: bool) -> str:
    """Public WebDAV href for a drive path (kept percent-encoded, `/dav`-prefixed,
    collections get a trailing slash)."""
    encoded = quote(drive_path, safe="/")
    href = DAV_PREFIX + encoded
    if is_folder and not href.endswith("/"):
        href += "/"
    return href


def _parse_destination(request: web.Request) -> str | None:
    """The `Destination` header (absolute URL or path) → a drive path, or None."""
    dest = request.headers.get("Destination")
    if not dest:
        return None
    path = urlsplit(dest).path  # drop scheme/host; keep the path
    return _normalize(unquote(path))


def _overwrite_allowed(request: web.Request) -> bool:
    """WebDAV `Overwrite` header: 'T' (default) allows clobbering, 'F' forbids."""
    return request.headers.get("Overwrite", "T").upper() != "F"


# -- PROPFIND XML -----------------------------------------------------------


def _httpdate(dt) -> str:
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def _etag(node: Node) -> str:
    mtime_us = int(node.mtime.timestamp() * 1_000_000)
    return f'"{node.id}-{node.size}-{mtime_us}"'


def _node_response(drive_path: str, node: Node) -> str:
    href = _href(drive_path, node.is_folder)
    name = escape(node.name)
    when = _httpdate(node.mtime)
    if node.is_folder:
        prop = (
            "<d:resourcetype><d:collection/></d:resourcetype>"
            f"<d:displayname>{name}</d:displayname>"
            f"<d:getlastmodified>{when}</d:getlastmodified>"
        )
    else:
        prop = (
            "<d:resourcetype/>"
            f"<d:displayname>{name}</d:displayname>"
            f"<d:getcontentlength>{node.size}</d:getcontentlength>"
            f"<d:getcontenttype>{escape(node.mime or 'application/octet-stream')}</d:getcontenttype>"
            f"<d:getlastmodified>{when}</d:getlastmodified>"
            f"<d:getetag>{escape(_etag(node))}</d:getetag>"
        )
    return (
        "<d:response>"
        f"<d:href>{escape(href)}</d:href>"
        f"<d:propstat><d:prop>{prop}</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        "</d:response>"
    )


def build_multistatus(entries: list[tuple[str, Node]]) -> bytes:
    """A 207 Multi-Status body for (drive_path, node) pairs."""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:">'
        + "".join(_node_response(p, n) for p, n in entries)
        + "</d:multistatus>"
    )
    return body.encode("utf-8")


# -- request body -----------------------------------------------------------


def _body_factory(request: web.Request) -> Callable[[], AsyncIterator[bytes]]:
    def factory() -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            while True:
                chunk = await request.content.read(_READ_CHUNK)
                if not chunk:
                    break
                yield chunk

        return gen()

    return factory


# -- method handlers --------------------------------------------------------


async def _options(request: web.Request, fs: FileSystem) -> web.Response:
    return web.Response(
        status=200,
        headers={"DAV": "1", "Allow": _ALLOW, "MS-Author-Via": "DAV"},
    )


async def _propfind(request: web.Request, fs: FileSystem) -> web.StreamResponse:
    drive_path = _drive_path(request)
    node = await fs.resolve(drive_path)
    if node is None:
        raise web.HTTPNotFound()

    depth = request.headers.get("Depth", "1").lower()
    entries: list[tuple[str, Node]] = [(drive_path, node)]
    if depth != "0" and node.is_folder:
        for child in await fs.list_children(node.id, state="ACTIVE"):
            entries.append((_child_path(drive_path, child.name), child))

    return web.Response(
        status=207,
        body=build_multistatus(entries),
        content_type="application/xml",
        charset="utf-8",
    )


async def _get(request: web.Request, fs: FileSystem) -> web.StreamResponse:
    node = await fs.resolve(_drive_path(request))
    if node is None or node.state != "ACTIVE":
        raise web.HTTPNotFound()
    if node.is_folder:
        raise web.HTTPMethodNotAllowed(request.method, [_ALLOW])
    return await stream_node(request, fs, node)


async def _put(request: web.Request, fs: FileSystem) -> web.Response:
    drive_path = _drive_path(request)
    parent_path, raw_name = _split_parent(drive_path)
    try:
        name = safe_filename(raw_name)
    except ValueError:
        raise web.HTTPBadRequest()

    parent = await fs.resolve(parent_path)
    if parent is None or not parent.is_folder:
        raise web.HTTPConflict()  # WebDAV: PUT under a missing collection → 409

    existing = await fs.repo.get_child_by_name(parent.id, name, state="ACTIVE")
    if existing is not None and existing.is_folder:
        raise web.HTTPMethodNotAllowed(request.method, [_ALLOW])
    overwrite = existing is not None

    mime = request.headers.get("Content-Type") or None
    if mime and mime.split(";")[0].strip() == "application/octet-stream":
        mime = None  # let fs.write deduce from the filename

    node = await fs.write(parent.id, name, _body_factory(request), mime=mime, overwrite=overwrite)
    log.info("[webdav] PUT %s (%d bytes)%s", drive_path, node.size, " [overwrite]" if overwrite else "")
    return web.Response(status=204 if overwrite else 201)


async def _mkcol(request: web.Request, fs: FileSystem) -> web.Response:
    if request.can_read_body and await request.read():
        raise web.HTTPUnsupportedMediaType()  # MKCOL with a body is undefined
    drive_path = _drive_path(request)
    parent_path, name = _split_parent(drive_path)
    parent = await fs.resolve(parent_path)
    if parent is None or not parent.is_folder:
        raise web.HTTPConflict()
    if await fs.repo.get_child_by_name(parent.id, name, state="ACTIVE") is not None:
        raise web.HTTPMethodNotAllowed(request.method, [_ALLOW])
    await fs.mkdir(parent.id, name)
    log.info("[webdav] MKCOL %s", drive_path)
    return web.Response(status=201)


async def _delete(request: web.Request, fs: FileSystem) -> web.Response:
    node = await fs.resolve(_drive_path(request))
    if node is None:
        raise web.HTTPNotFound()
    await fs.delete(node.id, purge=False)  # soft-delete; messages survive
    log.info("[webdav] DELETE %s", _drive_path(request))
    return web.Response(status=204)


async def _resolve_move_copy(request: web.Request, fs: FileSystem):
    """Shared prelude for MOVE/COPY: returns (src, dest_parent, dest_name,
    existing_dest) after validating source, destination and Overwrite. Raises the
    proper WebDAV error otherwise."""
    src = await fs.resolve(_drive_path(request))
    if src is None:
        raise web.HTTPNotFound()
    dest_path = _parse_destination(request)
    if dest_path is None or dest_path == "/":
        raise web.HTTPBadRequest()
    dest_parent_path, dest_name = _split_parent(dest_path)
    dest_parent = await fs.resolve(dest_parent_path)
    if dest_parent is None or not dest_parent.is_folder:
        raise web.HTTPConflict()
    existing = await fs.repo.get_child_by_name(dest_parent.id, dest_name, state="ACTIVE")
    if existing is not None:
        if not _overwrite_allowed(request):
            raise web.HTTPPreconditionFailed()
        if existing.id == src.id:
            existing = None  # moving/copying onto itself path-wise: ignore
    return src, dest_parent, dest_name, existing


async def _move(request: web.Request, fs: FileSystem) -> web.Response:
    src, dest_parent, dest_name, existing = await _resolve_move_copy(request, fs)
    await fs.ensure_move_allowed(src.id, dest_parent.id)
    created = existing is None
    if existing is not None:
        await fs.delete(existing.id, purge=False)
    try:
        if src.parent_id != dest_parent.id:
            await fs.move(src.id, dest_parent.id)
        if src.name != dest_name:
            await fs.rename(src.id, dest_name)
    except DuplicateNameError:
        raise web.HTTPPreconditionFailed()
    log.info("[webdav] MOVE %s → %s", _drive_path(request), dest_name)
    return web.Response(status=201 if created else 204)


async def _copy(request: web.Request, fs: FileSystem) -> web.Response:
    src, dest_parent, dest_name, existing = await _resolve_move_copy(request, fs)
    created = existing is None
    if existing is not None:
        await fs.delete(existing.id, purge=False)
    new = await fs.copy(src.id, dest_parent.id, force_copy=True)
    if new.name != dest_name:
        await fs.rename(new.id, dest_name)
    log.info("[webdav] COPY %s → %s", _drive_path(request), dest_name)
    return web.Response(status=201 if created else 204)


_HANDLERS: dict[str, Callable] = {
    "OPTIONS": _options,
    "PROPFIND": _propfind,
    "GET": _get,
    "HEAD": _get,
    "PUT": _put,
    "MKCOL": _mkcol,
    "DELETE": _delete,
    "MOVE": _move,
    "COPY": _copy,
}


def _capture_registration(request: web.Request) -> None:
    """Self-registration: if the request carries an authorised `X-Tgshelf-RC`
    header, (re)register that rc endpoint as a live push target. Never raises —
    the data-plane must not depend on the control-plane."""
    registry: RcRegistry | None = request.app[RUNTIME].get("rc_registry")
    rc_cfg = request.app[RUNTIME].get("rclone")
    if registry is None or rc_cfg is None:
        return
    rc_url = request.headers.get("X-Tgshelf-RC")
    if not rc_url:
        return
    if is_rc_authorized(
        rc_url,
        request.headers.get("X-Tgshelf-Token"),
        register_token=rc_cfg.register_token,
        allowed_networks=rc_cfg.allowed_rc_networks,
        source_ip=request.remote,
    ):
        registry.touch(rc_url)
        log.debug("[rcregistry] registered rc endpoint from %s", request.remote)
    else:
        log.debug("[rcregistry] rejected rc registration from %s", request.remote)


async def webdav_handler(request: web.Request) -> web.StreamResponse:
    _capture_registration(request)
    handler = _HANDLERS.get(request.method)
    if handler is None:
        raise web.HTTPMethodNotAllowed(request.method, list(_HANDLERS))
    try:
        async with open_fs(request) as fs:
            return await handler(request, fs)
    except NotAReadableFile:
        raise web.HTTPNotFound()
    except NotAFolder:
        raise web.HTTPConflict()
    except InvalidMove:
        raise web.HTTPConflict()
    except DuplicateNameError:
        raise web.HTTPPreconditionFailed()


def register_webdav_routes(app: web.Application) -> None:
    app.router.add_route("*", DAV_PREFIX, webdav_handler)
    app.router.add_route("*", DAV_PREFIX + "/{path:.*}", webdav_handler)
