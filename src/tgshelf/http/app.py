"""aiohttp application factory + middlewares.

Auth mirrors the legacy: basic auth, with a CIDR whitelist that BYPASSES it
(media players on the LAN can't do basic auth on .strm URLs). `/ping` is always
open. IPv6 / missing remote are handled gracefully (the legacy crashed on them).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import logging
from typing import Any

from aiohttp import web

from tgshelf.config import HttpConfig

log = logging.getLogger("tgshelf.http")

AUTH_EXEMPT_PATHS = frozenset({"/ping"})

# runtime components (session_factory, master_channel, executor, …) live under
# one typed AppKey — avoids aiohttp's deprecated str-keyed app storage
RUNTIME: "web.AppKey[dict]" = web.AppKey("tgshelf_runtime", dict)


def _client_ip(request: web.Request) -> ipaddress._BaseAddress | None:
    remote = request.remote
    if not remote:
        return None
    try:
        return ipaddress.ip_address(remote)
    except ValueError:  # IPv6 with scope, unix socket, etc.
        return None


def _check_basic_auth(header: str | None, user: str, password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    got_user, _, got_pass = decoded.partition(":")
    return got_user == user and got_pass == password


def make_auth_middleware(config: HttpConfig):
    networks = [ipaddress.ip_network(c, strict=False) for c in config.ignore_auth_for]
    auth_required = bool(config.user or config.password)

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if request.path in AUTH_EXEMPT_PATHS or not auth_required:
            return await handler(request)

        ip = _client_ip(request)
        if ip is not None and any(ip in net for net in networks):
            return await handler(request)  # whitelisted -> bypass

        if _check_basic_auth(
            request.headers.get("Authorization"), config.user, config.password
        ):
            return await handler(request)

        raise web.HTTPUnauthorized(
            headers={"WWW-Authenticate": 'Basic realm="tgshelf"'}
        )

    return auth_middleware


def _domain_status(exc: Exception) -> int | None:
    """Map a domain exception to an HTTP status (None = unhandled → 500)."""
    from tgshelf.core.download import RangeNotSatisfiable
    from tgshelf.core.fs import NotAReadableFile
    from tgshelf.db.repo import DuplicateNameError

    if isinstance(exc, DuplicateNameError):
        return 409
    if isinstance(exc, NotAReadableFile):
        return 404
    if isinstance(exc, RangeNotSatisfiable):
        return 416
    if isinstance(exc, ValueError):
        return 400
    return None


# a client closing the connection mid-response (very common for the interrupted
# range requests of a streaming player) surfaces as one of these; it is not a
# server fault, so it must not produce a 500 stacktrace.
_CLIENT_GONE = (ConnectionResetError, ConnectionError, asyncio.CancelledError)


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Pass HTTP responses through; map domain exceptions to status codes;
    anything else becomes a JSON 500."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except _CLIENT_GONE as exc:
        # client went away mid-response: log quietly (no stacktrace) and re-raise
        # so aiohttp tears the request down — the socket is gone, no 500 to send.
        log.debug("client disconnected on %s %s: %s",
                  request.method, request.path, exc.__class__.__name__)
        raise
    except Exception as exc:  # noqa: BLE001
        status = _domain_status(exc)
        if status is not None:
            return web.json_response({"error": str(exc)}, status=status)
        log.exception("unhandled error serving %s %s", request.method, request.path)
        return web.json_response({"error": str(exc)}, status=500)


async def _ping(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def make_app(config: HttpConfig, **extra: Any) -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, make_auth_middleware(config)],
        client_max_size=0,  # uploads stream; no in-memory body cap
    )
    app.router.add_get("/ping", _ping)
    app[RUNTIME] = dict(extra)
    return app
