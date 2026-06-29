"""JSON API routes (/api/v1): metadata + tree operations.

Each request gets its own AsyncSession and a FileSystem wired with the runtime
components stored on the app (session_factory, master_channel, executor, …).
Handlers raise domain exceptions; the error middleware maps them to status codes.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from aiohttp import web

from tgshelf.core.fs import FileSystem
from tgshelf.db.repo import NodeRepo
from tgshelf.http.app import RUNTIME
from tgshelf.http.schemas import node_to_dict

log = logging.getLogger("tgshelf.http.api")


def _runtime_fs(rt: dict, session) -> FileSystem:
    return FileSystem(
        NodeRepo(session),
        master_channel=rt["master_channel"],
        executor=rt.get("executor"),
        uploader=rt.get("uploader"),
        streamer=rt.get("streamer"),
        gateway=rt.get("gateway"),
        min_size=rt.get("min_size", 0),
        notifier=rt.get("notifier"),
    )


@asynccontextmanager
async def open_fs(request: web.Request):
    rt = request.app[RUNTIME]
    async with rt["session_factory"]() as session:
        yield _runtime_fs(rt, session)


def _spawn_background(app: web.Application, coro) -> None:
    """Fire-and-forget a background task, keeping a reference so it is not GC'd.
    No job tracking (user decision): a crash/restart loses pending work, but
    everything committed so far is durable (per-file commits are crash-safe)."""
    rt = app[RUNTIME]
    tasks = rt.setdefault("_bg_tasks", set())
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _run_op_background(rt: dict, op: str, node_id: str, parent_id: str) -> None:
    log.info("[bg] starting %s of folder %s -> %s", op, node_id, parent_id)
    try:
        async with rt["session_factory"]() as session:
            fs = _runtime_fs(rt, session)
            if op == "move":
                await fs.move(node_id, parent_id)
            else:
                await fs.copy(node_id, parent_id)
        log.info("[bg] %s of folder %s -> %s completed", op, node_id, parent_id)
    except Exception:  # noqa: BLE001 - fire-and-forget: log, never crash the loop
        log.exception("[bg] %s of folder %s -> %s FAILED", op, node_id, parent_id)


def _not_found(detail: str) -> web.Response:
    return web.json_response({"error": detail}, status=404)


def _bad_request(detail: str) -> web.Response:
    return web.json_response({"error": detail}, status=400)


async def _json_body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON
        raise web.HTTPBadRequest(
            text='{"error": "invalid JSON body"}', content_type="application/json"
        )
    return body if isinstance(body, dict) else {}


def _truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


_LIST_STATES = ("ACTIVE", "DELETED", "TEMP")


async def get_node(request: web.Request) -> web.Response:
    async with open_fs(request) as fs:
        node = await fs.get(request.match_info["id"])
        if node is None:
            return _not_found(f"node {request.match_info['id']} not found")
        return web.json_response(node_to_dict(node))


async def node_size(request: web.Request) -> web.Response:
    node_id = request.match_info["id"]
    async with open_fs(request) as fs:
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        total = await fs.total_size(node_id)
        return web.json_response(
            {"id": node.id, "is_folder": node.is_folder, "size": total}
        )


async def list_children(request: web.Request) -> web.Response:
    state = request.query.get("state", "ACTIVE")
    if state not in _LIST_STATES:
        return _bad_request(f"state must be one of {', '.join(_LIST_STATES)}")
    async with open_fs(request) as fs:
        nodes = await fs.list_children(request.match_info["id"], state=state)
        return web.json_response([node_to_dict(n) for n in nodes])


async def resolve(request: web.Request) -> web.Response:
    path = request.query.get("path", "")
    async with open_fs(request) as fs:
        node = await fs.resolve(path)
        if node is None:
            return _not_found(f"path '{path}' not found")
        return web.json_response(node_to_dict(node))


async def search(request: web.Request) -> web.Response:
    term = request.query.get("q", "")
    root_id = request.query.get("root")
    async with open_fs(request) as fs:
        nodes = await fs.search(term, root_id=root_id)
        return web.json_response([node_to_dict(n) for n in nodes])


# -- write: tree --------------------------------------------------------------


async def create_folder(request: web.Request) -> web.Response:
    body = await _json_body(request)
    async with open_fs(request) as fs:
        if body.get("path"):
            node = await fs.mkdirs(body["path"])
        elif body.get("parent_id") and body.get("name"):
            node = await fs.mkdir(body["parent_id"], body["name"])
        else:
            return _bad_request("provide 'path', or 'parent_id' and 'name'")
        log.info("created folder %s (%s)", node.id, node.name)
        return web.json_response(node_to_dict(node), status=201)


async def update_node(request: web.Request) -> web.Response:
    """PUT contract: file -> name + mime (empty mime deduced from the name);
    folder -> name + channel_id. A file's channel is immutable here (use move)."""
    node_id = request.match_info["id"]
    body = await _json_body(request)
    async with open_fs(request) as fs:
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        if not node.is_folder and "channel_id" in body:
            return _bad_request("a file's channel can only be changed by moving it")

        if body.get("name"):
            await fs.rename(node_id, body["name"])
        if node.is_folder and "channel_id" in body:  # null = inherit
            await fs.set_channel(node_id, body["channel_id"])
        if not node.is_folder and "mime" in body:  # empty -> deduced from the name
            await fs.set_mime(node_id, body["mime"])
        log.info("updated node %s %s", node_id, sorted(body))
        return web.json_response(node_to_dict(await fs.get(node_id)))


_MAX_INLINE_EDIT = 256 * 1024 * 1024  # cap the in-memory read for an edit


async def replace_content(request: web.Request) -> web.Response:
    """PUT raw body = the new file body. Only INLINE (DB-stored) files are
    editable; a body within min_size stays inline, a larger one needs `?force=`
    to convert the file to Telegram-backed (fs raises InlineTooLarge → 409
    otherwise). Telegram-backed files are rejected (ValueError → 400)."""
    node_id = request.match_info["id"]
    force = _truthy(request.query.get("force", ""))
    if request.content_length is not None and request.content_length > _MAX_INLINE_EDIT:
        return _bad_request(f"content exceeds the {_MAX_INLINE_EDIT}-byte edit limit")
    data = await request.read()
    async with open_fs(request) as fs:
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        if node.is_folder:
            return _bad_request("a folder has no content")
        updated = await fs.replace_content(node_id, data, force=force)
        log.info("edited content of %s (%d bytes, force=%s)", node_id, len(data), force)
        return web.json_response(node_to_dict(updated))


async def delete_node(request: web.Request) -> web.Response:
    node_id = request.match_info["id"]
    purge = _truthy(request.query.get("purge", ""))
    async with open_fs(request) as fs:
        if await fs.get(node_id) is None:
            return _not_found(f"node {node_id} not found")
        await fs.delete(node_id, purge=purge)
        log.info("deleted node %s (purge=%s)", node_id, purge)
        return web.json_response({"ok": True, "purged": purge})


async def restore_node(request: web.Request) -> web.Response:
    node_id = request.match_info["id"]
    async with open_fs(request) as fs:
        await fs.restore(node_id)
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        log.info("restored node %s", node_id)
        return web.json_response(node_to_dict(node))


async def move_node(request: web.Request) -> web.Response:
    node_id = request.match_info["id"]
    body = await _json_body(request)
    parent_id = body.get("parent_id")
    if not parent_id:
        return _bad_request("'parent_id' is required")
    async with open_fs(request) as fs:
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        await fs.ensure_move_target(parent_id)  # sync 400/404 before any 202
        if node.is_folder:  # may take hours -> fire-and-forget, respond now
            log.info("move folder %s -> %s: accepted (background)", node_id, parent_id)
            _spawn_background(request.app, _run_op_background(request.app[RUNTIME], "move", node_id, parent_id))
            return web.json_response(
                {"status": "accepted", "operation": "move", "node_id": node_id}, status=202
            )
        log.info("move file %s -> %s", node_id, parent_id)
        return web.json_response(node_to_dict(await fs.move(node_id, parent_id)))


async def copy_node(request: web.Request) -> web.Response:
    node_id = request.match_info["id"]
    body = await _json_body(request)
    parent_id = body.get("parent_id")
    if not parent_id:
        return _bad_request("'parent_id' is required")
    async with open_fs(request) as fs:
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        await fs.ensure_move_target(parent_id)  # sync 400/404 before any 202
        if node.is_folder:  # may take hours -> fire-and-forget, respond now
            log.info("copy folder %s -> %s: accepted (background)", node_id, parent_id)
            _spawn_background(request.app, _run_op_background(request.app[RUNTIME], "copy", node_id, parent_id))
            return web.json_response(
                {"status": "accepted", "operation": "copy", "node_id": node_id}, status=202
            )
        log.info("copy file %s -> %s", node_id, parent_id)
        return web.json_response(node_to_dict(await fs.copy(node_id, parent_id)), status=201)


async def merge_node(request: web.Request) -> web.Response:
    """Stitch donor files' parts into the target file, hard-deleting the donors.

    Body supports the legacy append form `{donor_ids:[...]}` and the Web UI form:
    `{donor_ids:[...], name?: str, parts?: [{file_id, idx}, ...]}` where `parts`
    is the exact output order across target and donors.
    """
    node_id = request.match_info["id"]
    body = await _json_body(request)
    donor_ids = body.get("donor_ids")
    if not isinstance(donor_ids, list) or not donor_ids:
        return _bad_request("'donor_ids' must be a non-empty list")
    if node_id in donor_ids:
        return _bad_request("a node cannot be a donor of itself")
    name = body.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        return _bad_request("'name' must be a non-empty string")
    parts = body.get("parts")
    if parts is not None:
        if not isinstance(parts, list) or not parts:
            return _bad_request("'parts' must be a non-empty list")
        for part in parts:
            if (
                not isinstance(part, dict)
                or not isinstance(part.get("file_id"), str)
                or not isinstance(part.get("idx"), int)
            ):
                return _bad_request("'parts' entries must contain file_id and idx")
    async with open_fs(request) as fs:
        target = await fs.get(node_id)
        if target is None:
            return _not_found(f"node {node_id} not found")
        if target.is_folder:
            return _bad_request("cannot merge into a folder")
        for did in donor_ids:
            donor = await fs.get(did)
            if donor is None:
                return _bad_request(f"donor {did} not found")
            if donor.is_folder:
                return _bad_request(f"donor {did} is a folder")
        merged = await fs.merge_parts(
            node_id,
            donor_ids,
            name=name.strip() if isinstance(name, str) else None,
            part_refs=parts,
        )
        log.info("merged %d donor(s) into %s", len(donor_ids), node_id)
        return web.json_response(node_to_dict(merged))


async def strm_node(request: web.Request) -> web.Response:
    """Generate .strm for one folder/file into the global strm.destination tree
    at its correct relative position (partial in-place regen — no reprocessing of
    the parent). Synchronous (strm is local-only). Body: {clear?: bool}."""
    from tgshelf.commands import strm as strm_cmd

    node_id = request.match_info["id"]
    body = await _json_body(request)
    clear = bool(body.get("clear", False))
    cfg = request.app[RUNTIME].get("strm")
    if cfg is None:
        return _bad_request("strm is not configured")
    async with open_fs(request) as fs:
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        node_path = await fs.path_of(node_id)
        base = strm_cmd.strm_base(cfg.destination, cfg.source, node_path, node.is_folder)
        if base is None:
            return _bad_request(f"node is not under strm.source ({cfg.source})")
        # clear only makes sense for a folder; for a file it would wipe siblings
        stats = await strm_cmd.generate(
            fs.repo, node, base, cfg.template, clear=clear and node.is_folder
        )
        log.info("strm generated for %s -> %s (%s)", node_id, base, stats)
        return web.json_response(
            {
                "destination": str(base),
                "created": stats.created,
                "updated": stats.updated,
                "skipped": stats.skipped,
                "removed": stats.removed,
                "inline": stats.inline,
            }
        )


async def list_parts(request: web.Request) -> web.Response:
    """Ordered parts of a multi-part file (for the WebUI reorder view)."""
    node_id = request.match_info["id"]
    async with open_fs(request) as fs:
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        if node.is_folder:
            return _bad_request("a folder has no parts")
        parts = await fs.repo.parts_of(node_id)
        return web.json_response([
            {"idx": p.idx, "size": p.size, "original_filename": p.original_filename}
            for p in parts
        ])


async def reorder_parts_node(request: web.Request) -> web.Response:
    """Re-sequence the parts of one file. Body `{order:[int,...]}` = a permutation
    of the current part indices. fs.reorder_parts raises ValueError (inline / no
    parts / non-permutation) → 400 via the error middleware."""
    node_id = request.match_info["id"]
    body = await _json_body(request)
    order = body.get("order")
    if not isinstance(order, list) or not order or not all(isinstance(i, int) for i in order):
        return _bad_request("'order' must be a non-empty list of integers")
    async with open_fs(request) as fs:
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        if node.is_folder:
            return _bad_request("a folder has no parts to reorder")
        updated = await fs.reorder_parts(node_id, order)
        log.info("reordered parts of %s -> %s", node_id, order)
        return web.json_response(node_to_dict(updated))


async def split_parts_node(request: web.Request) -> web.Response:
    """Extract selected parts into one-part sibling files.

    Body `{part_indices:[int,...]}` names the current ordered part positions to
    extract. The source keeps every unselected part.
    """
    node_id = request.match_info["id"]
    body = await _json_body(request)
    part_indices = body.get("part_indices")
    if (
        not isinstance(part_indices, list)
        or not part_indices
        or not all(isinstance(i, int) for i in part_indices)
    ):
        return _bad_request("'part_indices' must be a non-empty list of integers")
    async with open_fs(request) as fs:
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        if node.is_folder:
            return _bad_request("a folder has no parts to split")
        source, extracted = await fs.split_parts(node_id, part_indices)
        log.info("split %d part(s) from %s", len(extracted), node_id)
        return web.json_response(
            {
                "source": node_to_dict(source),
                "extracted": [node_to_dict(node) for node in extracted],
            }
        )


def register_routes(app: web.Application) -> None:
    app.router.add_get("/api/v1/nodes/{id}", get_node)
    app.router.add_get("/api/v1/nodes/{id}/children", list_children)
    app.router.add_get("/api/v1/nodes/{id}/size", node_size)
    app.router.add_get("/api/v1/resolve", resolve)
    app.router.add_get("/api/v1/search", search)
    app.router.add_post("/api/v1/folders", create_folder)
    app.router.add_put("/api/v1/nodes/{id}", update_node)
    app.router.add_put("/api/v1/nodes/{id}/content", replace_content)
    app.router.add_delete("/api/v1/nodes/{id}", delete_node)
    app.router.add_post("/api/v1/nodes/{id}/restore", restore_node)
    app.router.add_post("/api/v1/nodes/{id}/move", move_node)
    app.router.add_post("/api/v1/nodes/{id}/copy", copy_node)
    app.router.add_post("/api/v1/nodes/{id}/merge", merge_node)
    app.router.add_get("/api/v1/nodes/{id}/parts", list_parts)
    app.router.add_post("/api/v1/nodes/{id}/parts/split", split_parts_node)
    app.router.add_put("/api/v1/nodes/{id}/parts", reorder_parts_node)
    app.router.add_post("/api/v1/nodes/{id}/strm", strm_node)
