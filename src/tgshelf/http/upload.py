r"""Upload endpoint — `POST /api/v1/folders/{id}/files`.

The filename is NEVER taken from the URL path (a name may legally contain
`* _ { } [ ]` and a path separator `/`, which a URL path can't carry safely).
It comes from the request content instead:

- **multipart/form-data**: the file part's own `filename` (the multipart spec's
  dedicated, already-escaped carrier);
- **raw body**: the `?filename=` query (URL-encoded) or the `Content-Disposition`
  header (`filename` / RFC 5987 `filename*`).

`safe_filename` rejects only what is structurally dangerous (empty, `/`/`\`,
control chars, `.`/`..`) — the legal-but-special chars are kept verbatim (stored
as text; download resolves by file_id, never by name). Streams straight into
`fs.write`; a same-name ACTIVE sibling is pre-checked → 409 (no wasted upload).
"""

from __future__ import annotations

import logging
import re
from typing import Any, AsyncIterator, Callable
from urllib.parse import unquote

from aiohttp import web

from tgshelf.db.repo import DuplicateNameError
from tgshelf.http.api import open_fs
from tgshelf.http.schemas import node_to_dict

log = logging.getLogger("tgshelf.http.upload")

_READ_CHUNK = 64 * 1024


def safe_filename(name: str) -> str:
    """Validate a single-segment filename; raise ValueError (→ 400) otherwise.

    Rejects only structural hazards; legal special chars (* _ { } [ ]) pass."""
    name = (name or "").strip()
    if not name or name in (".", ".."):
        raise ValueError("filename is required")
    if "/" in name or "\\" in name:
        raise ValueError("filename must not contain a path separator")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("filename must not contain control characters")
    return name


def _clean_mime(ctype: str | None) -> str | None:
    """A meaningful Content-Type, else None so fs.write deduces from the name.
    A generic octet-stream is treated as 'unspecified' to enable deduction."""
    if not ctype:
        return None
    ctype = ctype.split(";")[0].strip()
    if not ctype or ctype == "application/octet-stream":
        return None
    return ctype


def _filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", header)
    if m:
        return unquote(m.group(1).strip())
    m = re.search(r'filename\s*=\s*"([^"]*)"', header)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*([^;]+)", header)
    if m:
        return m.group(1).strip()
    return None


def _raw_factory(request: web.Request) -> Callable[[], AsyncIterator[bytes]]:
    def factory() -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            while True:
                chunk = await request.content.read(_READ_CHUNK)
                if not chunk:
                    break
                yield chunk

        return gen()

    return factory


def _part_factory(part: Any) -> Callable[[], AsyncIterator[bytes]]:
    def factory() -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            while True:
                chunk = await part.read_chunk(_READ_CHUNK)
                if not chunk:
                    break
                yield chunk

        return gen()

    return factory


async def _resolve_source(request: web.Request):
    """Return (name, mime, source_factory) from the request, by content type."""
    if request.content_type == "multipart/form-data":
        reader = await request.multipart()
        async for part in reader:
            if part.filename is not None:  # the file part (skips form fields)
                return part.filename, _clean_mime(part.headers.get("Content-Type")), _part_factory(part)
        raise web.HTTPBadRequest(
            text='{"error": "no file part in multipart body"}',
            content_type="application/json",
        )

    name = request.query.get("filename") or _filename_from_content_disposition(
        request.headers.get("Content-Disposition")
    )
    if not name:
        raise web.HTTPBadRequest(
            text='{"error": "filename required: use ?filename= or Content-Disposition"}',
            content_type="application/json",
        )
    return name, _clean_mime(request.headers.get("Content-Type")), _raw_factory(request)


async def upload_file(request: web.Request) -> web.Response:
    folder_id = request.match_info["id"]
    async with open_fs(request) as fs:
        parent = await fs.get(folder_id)
        if parent is None or not parent.is_folder:
            return web.json_response(
                {"error": f"folder {folder_id} not found"}, status=404
            )

        name, mime, factory = await _resolve_source(request)
        name = safe_filename(name)  # ValueError -> 400 (error middleware)

        existing = await fs.repo.get_child_by_name(folder_id, name)
        if existing is not None and existing.state == "ACTIVE":
            raise DuplicateNameError(f"'{name}' already exists in folder {folder_id}")

        node = await fs.write(folder_id, name, factory, mime=mime)
        log.info(
            "uploaded file %s '%s' (%d bytes) to %s", node.id, node.name, node.size, folder_id
        )
        return web.json_response(node_to_dict(node), status=201)


def register_upload_routes(app: web.Application) -> None:
    app.router.add_post("/api/v1/folders/{id}/files", upload_file)
