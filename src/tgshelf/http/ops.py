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


# -- /metrics (Prometheus text exposition, no extra dependency) --------------

_PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _metrics_text(rt: dict) -> str:
    clients = _pool_status(rt.get("client_pool"))
    bots = _pool_status(rt.get("bot_pool"))
    lines: list[str] = []

    def gauge(name: str, help_: str, samples: list[tuple[str, float]]) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            suffix = f"{{{labels}}}" if labels else ""
            lines.append(f"{name}{suffix} {value}")

    def counter(name: str, help_: str, value: float) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")

    for field, help_ in (
        ("total", "Configured pool members."),
        ("available", "Healthy members (not quarantined, not in cooldown)."),
        ("in_flight", "In-flight operations across the pool."),
    ):
        gauge(
            f"tgshelf_pool_{'members' if field == 'total' else field}",
            help_,
            [('pool="clients"', clients[field]), ('pool="bots"', bots[field])],
        )

    s = rt.get("streamer")
    m = s.metrics() if s is not None else {}
    gauge("tgshelf_stream_active", "Currently active download streams.",
          [("", m.get("active_streams", 0))])
    gauge("tgshelf_stream_buffered_bytes", "Estimated buffered bytes across active streams.",
          [("", m.get("buffered_bytes", 0))])
    gauge("tgshelf_stream_memory_soft_limit_bytes", "Soft buffer limit (0 = disabled).",
          [("", m.get("memory_soft_limit", 0))])
    counter("tgshelf_streams_total", "Download streams started.", m.get("streams_total", 0))
    counter("tgshelf_stream_bytes_total", "Bytes emitted to clients.", m.get("bytes_total", 0))
    counter("tgshelf_stream_degraded_total",
            "Streams started at K=1 by the soft limit.", m.get("degraded_total", 0))

    return "\n".join(lines) + "\n"


def _metrics_json(rt: dict) -> dict:
    """Same data as the Prometheus exposition, structured for the WebUI: pool
    health (full members, like /status) + streamer counters/gauges."""
    s = rt.get("streamer")
    return {
        "pools": {
            "clients": _pool_status(rt.get("client_pool")),
            "bots": _pool_status(rt.get("bot_pool")),
        },
        "stream": s.metrics() if s is not None else {},
    }


async def metrics(request: web.Request) -> web.Response:
    """JSON by default (the WebUI just does GET /metrics)."""
    return web.json_response(_metrics_json(request.app[RUNTIME]))


async def metrics_text(request: web.Request) -> web.Response:
    """`GET /metrics.txt` — Prometheus text exposition (for scrapers). The `.txt`
    URL suffix is our text opt-in; everything else stays JSON."""
    body = _metrics_text(request.app[RUNTIME]).encode("utf-8")
    return web.Response(body=body, headers={"Content-Type": _PROM_CONTENT_TYPE})


def register_ops_routes(app: web.Application) -> None:
    app.router.add_get("/status", status)
    app.router.add_get("/metrics", metrics)            # JSON
    app.router.add_get("/metrics.txt", metrics_text)   # Prometheus text
