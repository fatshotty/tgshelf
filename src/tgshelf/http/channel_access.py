"""Authenticated Web UI policy editor. No account credentials cross this API."""
from aiohttp import web

from tgshelf.core.access import AccessUpdate
from tgshelf.http.api import _json_body
from tgshelf.http.app import RUNTIME


async def list_access(request):
    return web.json_response(await request.app[RUNTIME]["access_service"].list_grid())


async def update_access(request):
    try:
        channel = int(request.match_info["channel_id"])
    except ValueError:
        raise ValueError("channel_id must be an integer") from None
    if not -(2**63) <= channel < 2**63:
        raise ValueError("channel_id is outside the signed 64-bit range")
    body = await _json_body(request)
    if not isinstance(body, dict) or set(body) != {"permissions"} or not isinstance(body["permissions"], list):
        raise ValueError("Expected a permissions array")
    updates = []
    for cell in body["permissions"]:
        if not isinstance(cell, dict) or set(cell) != {"account_name", "mode"}:
            raise ValueError("Each permission requires only account_name and mode")
        if not isinstance(cell["account_name"], str):
            raise ValueError("account_name must be a string")
        updates.append(AccessUpdate(cell["account_name"], cell["mode"]))
    return web.json_response(await request.app[RUNTIME]["access_service"].update(channel, updates))


def register_channel_access_routes(app):
    app.router.add_get("/api/v1/channel-access", list_access)
    app.router.add_put("/api/v1/channel-access/{channel_id}", update_access)
