"""Operational endpoints (root path, not /api/v1): `GET /status`.

`/status` is a read-only health snapshot of the two pools (user clients +
download bots): per-account in_flight / load / cooldown / quarantine / per-channel
ineligibility, plus a small pool-level summary. No DB access. `/metrics`
(Prometheus) lands here too in the next sub-point.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tgshelf.http.app import RUNTIME

log = logging.getLogger("tgshelf.http.ops")


def _member_status(m: Any, now: float) -> dict:
    cooldown = max(0.0, m.cooldown_until - now)
    return {
        "name": m.name,
        "is_premium": m.is_premium,
        "capacity": m.capacity,
        "in_flight": m.in_flight,
        "load": m.load,
        "quarantined": m.quarantined,
        "cooldown_remaining": round(cooldown, 3),
        "consecutive_errors": m.consecutive_errors,
        "ineligible_channels": sorted(m.ineligible_channels),
        # healthy = not quarantined, not in cooldown (channel-agnostic view)
        "available": not m.quarantined and cooldown == 0.0,
    }


def _pool_status(pool: Any) -> dict:
    if pool is None:
        return {"total": 0, "available": 0, "in_flight": 0, "members": []}
    now = pool.now()
    members = [_member_status(m, now) for m in pool.members]
    return {
        "total": len(members),
        "available": sum(1 for m in members if m["available"]),
        "in_flight": sum(m["in_flight"] for m in members),
        "members": members,
    }


async def status(request: web.Request) -> web.Response:
    rt = request.app[RUNTIME]
    return web.json_response(
        {
            "clients": _pool_status(rt.get("client_pool")),
            "bots": _pool_status(rt.get("bot_pool")),
        }
    )


def register_ops_routes(app: web.Application) -> None:
    app.router.add_get("/status", status)
