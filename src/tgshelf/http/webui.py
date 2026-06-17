"""Serve the built React SPA (single origin).

The Vite build lands in `src/tgshelf/webui/static/` (committed) and is served at
`/`: `index.html` + hashed `/assets/*`, with a SPA fallback so client-side routes
resolve to `index.html`. Auth (see `app._is_protected`) leaves the shell + assets
public; the SPA carries Basic auth on the API/download calls.

`register_webui_routes` MUST be called LAST (after the API/download/ops routes):
the catch-all `/{tail:.*}` would otherwise shadow them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

log = logging.getLogger("tgshelf.http.webui")

STATIC_DIR = Path(__file__).resolve().parent.parent / "webui" / "static"
INDEX = STATIC_DIR / "index.html"

# paths owned by the backend (or static assets) — never served the SPA shell
_NON_SPA_PREFIXES = ("/api/", "/download", "/status", "/metrics", "/ping", "/assets/")


async def _index(request: web.Request) -> web.StreamResponse:
    if not INDEX.is_file():
        return web.json_response(
            {"error": "webui not built", "hint": "cd webui && npm run build"},
            status=404,
        )
    return web.FileResponse(INDEX)


async def _spa_fallback(request: web.Request) -> web.StreamResponse:
    # explicit API/static routes win (registered first); this only catches
    # unknown paths. Guard the backend prefixes so they 404 as JSON, not HTML.
    if any(request.path.startswith(p) for p in _NON_SPA_PREFIXES):
        return web.json_response({"error": f"not found: {request.path}"}, status=404)
    return await _index(request)


def register_webui_routes(app: web.Application) -> None:
    app.router.add_get("/", _index)
    assets = STATIC_DIR / "assets"
    if assets.is_dir():  # only when the SPA has been built
        app.router.add_static("/assets", assets)
    app.router.add_get("/{tail:.*}", _spa_fallback)  # SPA fallback — keep LAST
