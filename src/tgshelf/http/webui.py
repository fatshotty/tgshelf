"""Serve the built React SPA (single origin).

The Vite build lands in `src/tgshelf/webui/static/` (committed) and is served at
`/webui`: `index.html` + hashed `/assets/*`, with a SPA fallback under
`/webui/...` so client-side routes resolve to `index.html`. `/` and the legacy
`/b/...` browse routes redirect to `/webui`.

`register_webui_routes` MUST be called LAST (after the API/download/ops routes):
the catch-all JSON 404 would otherwise shadow them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

log = logging.getLogger("tgshelf.http.webui")

STATIC_DIR = Path(__file__).resolve().parent.parent / "webui" / "static"
INDEX = STATIC_DIR / "index.html"

async def _index(request: web.Request) -> web.StreamResponse:
    if not INDEX.is_file():
        return web.json_response(
            {"error": "webui not built", "hint": "cd webui && npm run build"},
            status=404,
        )
    return web.FileResponse(INDEX)


def _append_query(location: str, request: web.Request) -> str:
    return f"{location}?{request.query_string}" if request.query_string else location


async def _root_redirect(request: web.Request) -> web.StreamResponse:
    raise web.HTTPPermanentRedirect(location=_append_query("/webui", request))


async def _legacy_browse_redirect(request: web.Request) -> web.StreamResponse:
    tail = request.match_info.get("tail", "")
    location = f"/webui/{tail}" if tail else "/webui"
    raise web.HTTPPermanentRedirect(location=_append_query(location, request))


async def _legacy_search_redirect(request: web.Request) -> web.StreamResponse:
    raise web.HTTPPermanentRedirect(location=_append_query("/webui/search", request))


async def _legacy_stats_redirect(request: web.Request) -> web.StreamResponse:
    raise web.HTTPPermanentRedirect(location=_append_query("/webui/stats", request))


async def _spa_fallback(request: web.Request) -> web.StreamResponse:
    return await _index(request)


async def _json_404(request: web.Request) -> web.Response:
    return web.json_response({"error": f"not found: {request.path}"}, status=404)


def register_webui_routes(app: web.Application) -> None:
    app.router.add_get("/", _root_redirect)
    assets = STATIC_DIR / "assets"
    if assets.is_dir():  # only when the SPA has been built
        app.router.add_static("/assets", assets)
    app.router.add_get("/webui", _index)
    app.router.add_get("/webui/{tail:.*}", _spa_fallback)
    app.router.add_get("/b", _legacy_browse_redirect)
    app.router.add_get("/b/{tail:.*}", _legacy_browse_redirect)
    app.router.add_get("/search", _legacy_search_redirect)
    app.router.add_get("/stats", _legacy_stats_redirect)
    app.router.add_get("/{tail:.*}", _json_404)  # keep LAST
