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


def register_routes(app: web.Application) -> None:
    app.router.add_get("/api/v1/nodes/{id}", get_node)
    app.router.add_get("/api/v1/nodes/{id}/children", list_children)
    app.router.add_get("/api/v1/resolve", resolve)
    app.router.add_get("/api/v1/search", search)
