"""JSON API routes (/api/v1): metadata + tree operations.

Each request gets its own AsyncSession and a FileSystem wired with the runtime
components stored on the app (session_factory, master_channel, executor, …).
Handlers raise domain exceptions; the error middleware maps them to status codes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from aiohttp import web

from tgshelf.core.fs import FileSystem
from tgshelf.db.repo import NodeRepo
from tgshelf.http.app import RUNTIME
from tgshelf.http.schemas import node_to_dict


@asynccontextmanager
async def open_fs(request: web.Request):
    rt = request.app[RUNTIME]
    async with rt["session_factory"]() as session:
        yield FileSystem(
            NodeRepo(session),
            master_channel=rt["master_channel"],
            executor=rt.get("executor"),
            uploader=rt.get("uploader"),
            streamer=rt.get("streamer"),
            gateway=rt.get("gateway"),
            min_size=rt.get("min_size", 0),
        )


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


async def get_node(request: web.Request) -> web.Response:
    async with open_fs(request) as fs:
        node = await fs.get(request.match_info["id"])
        if node is None:
            return _not_found(f"node {request.match_info['id']} not found")
        return web.json_response(node_to_dict(node))


async def list_children(request: web.Request) -> web.Response:
    async with open_fs(request) as fs:
        nodes = await fs.list_children(request.match_info["id"])
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
        return web.json_response(node_to_dict(await fs.get(node_id)))


async def delete_node(request: web.Request) -> web.Response:
    node_id = request.match_info["id"]
    purge = _truthy(request.query.get("purge", ""))
    async with open_fs(request) as fs:
        if await fs.get(node_id) is None:
            return _not_found(f"node {node_id} not found")
        await fs.delete(node_id, purge=purge)
        return web.json_response({"ok": True, "purged": purge})


async def restore_node(request: web.Request) -> web.Response:
    node_id = request.match_info["id"]
    async with open_fs(request) as fs:
        await fs.restore(node_id)
        node = await fs.get(node_id)
        if node is None:
            return _not_found(f"node {node_id} not found")
        return web.json_response(node_to_dict(node))


async def move_node(request: web.Request) -> web.Response:
    node_id = request.match_info["id"]
    body = await _json_body(request)
    parent_id = body.get("parent_id")
    if not parent_id:
        return _bad_request("'parent_id' is required")
    async with open_fs(request) as fs:
        if await fs.get(node_id) is None:
            return _not_found(f"node {node_id} not found")
        moved = await fs.move(node_id, parent_id)
        return web.json_response(node_to_dict(moved))


async def copy_node(request: web.Request) -> web.Response:
    node_id = request.match_info["id"]
    body = await _json_body(request)
    parent_id = body.get("parent_id")
    if not parent_id:
        return _bad_request("'parent_id' is required")
    async with open_fs(request) as fs:
        if await fs.get(node_id) is None:
            return _not_found(f"node {node_id} not found")
        new = await fs.copy(node_id, parent_id)
        return web.json_response(node_to_dict(new), status=201)


def register_routes(app: web.Application) -> None:
    app.router.add_get("/api/v1/nodes/{id}", get_node)
    app.router.add_get("/api/v1/nodes/{id}/children", list_children)
    app.router.add_get("/api/v1/resolve", resolve)
    app.router.add_get("/api/v1/search", search)
    app.router.add_post("/api/v1/folders", create_folder)
    app.router.add_put("/api/v1/nodes/{id}", update_node)
    app.router.add_delete("/api/v1/nodes/{id}", delete_node)
    app.router.add_post("/api/v1/nodes/{id}/restore", restore_node)
    app.router.add_post("/api/v1/nodes/{id}/move", move_node)
    app.router.add_post("/api/v1/nodes/{id}/copy", copy_node)
