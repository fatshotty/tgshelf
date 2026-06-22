"""rclone control-plane bridge: changes feed → `vfs/forget` push.

Listens on the Postgres `changes` channel (the trigger's pg_notify, enriched with
parent ids in rev 0002) and, for every event, invalidates the affected
directory(ies) in the VFS cache of every registered rclone mount — so a new
file/folder shows up at the next `ls` without waiting for `--dir-cache-time`.

Best-effort and NEVER fatal to `serve` (same contract as the channel watcher): a
dropped LISTEN connection reconnects with backoff; a failing rc endpoint is logged
and skipped. The pure decision (which dirs, which rc URL/auth) is split out so it
unit-tests without a loop or a network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import asyncpg

from tgshelf.constants import ROOT_ID
from tgshelf.db.repo import NodeRepo
from tgshelf.http.rcregistry import RcRegistry

log = logging.getLogger("tgshelf.rcbridge")

_FORGET_TIMEOUT = 5.0
_BACKOFF_MAX = 30.0


def asyncpg_dsn(db_url: str) -> str:
    """SQLAlchemy DSN → a plain libpq DSN asyncpg.connect accepts."""
    return db_url.replace("+asyncpg", "")


def rc_dir(path: str) -> str:
    """A drive path ('/a/b', '/') → rclone's mount-relative dir ('a/b', '')."""
    return path.strip("/")


def split_rc_auth(rc_url: str) -> tuple[str, aiohttp.BasicAuth | None]:
    """Split an rc URL into (base_without_credentials, BasicAuth|None).

    Credentials may be embedded (http://user:pass@host:5572 = rclone --rc-user/
    --rc-pass); aiohttp won't use URL credentials on its own, so we extract them.
    """
    parts = urlsplit(rc_url)
    auth = None
    netloc = parts.netloc
    if parts.username is not None:
        auth = aiohttp.BasicAuth(parts.username, parts.password or "")
        host = parts.hostname or ""
        netloc = f"{host}:{parts.port}" if parts.port else host
    base = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return base.rstrip("/"), auth


class RcBridge:
    def __init__(self, dsn: str, session_factory: Any, registry: RcRegistry):
        self._dsn = dsn
        self._session_factory = session_factory
        self._registry = registry
        self._http: aiohttp.ClientSession | None = None
        self._reconnect = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    # -- event handling (testable core) ------------------------------------

    async def dirs_to_forget(self, event: dict) -> list[str]:
        """The mount-relative dirs to invalidate for one feed event: the node's
        parent dir, plus (on MOVE) its old parent dir. Resolved via path_of so a
        purged node (whose row is gone) is never needed — only its parents, which
        survive. Deduped, order preserved."""
        ids = [event.get("parent_id"), event.get("old_parent_id")]
        dirs: list[str] = []
        async with self._session_factory() as session:
            repo = NodeRepo(session)
            for pid in ids:
                if not pid:
                    continue
                path = "/" if pid == ROOT_ID else await repo.path_of(pid)
                if path is not None:
                    dirs.append(rc_dir(path))
        return list(dict.fromkeys(dirs))

    async def broadcast_forget(self, dirs: list[str]) -> None:
        """POST `vfs/forget` for each dir to every live rc endpoint (isolated:
        one failing endpoint/dir never blocks the others)."""
        endpoints = self._registry.live_endpoints()
        if not endpoints or not dirs:
            return
        assert self._http is not None
        for rc_url in endpoints:
            base, auth = split_rc_auth(rc_url)
            for d in dirs:
                try:
                    async with self._http.post(
                        f"{base}/vfs/forget",
                        json={"dir": d},
                        auth=auth,
                        timeout=aiohttp.ClientTimeout(total=_FORGET_TIMEOUT),
                    ) as resp:
                        await resp.read()
                        if resp.status >= 400:
                            log.warning("[rcbridge] forget %s dir=%r → HTTP %d", base, d, resp.status)
                        else:
                            log.debug("[rcbridge] forget %s dir=%r ok", base, d)
                except Exception as exc:  # noqa: BLE001 - per-endpoint isolation
                    log.warning("[rcbridge] forget %s dir=%r failed: %s", base, d, exc)

    async def handle_event(self, payload: str) -> None:
        try:
            event = json.loads(payload)
        except (ValueError, TypeError):
            log.debug("[rcbridge] ignoring malformed payload: %r", payload)
            return
        if not self._registry.live_endpoints():
            return  # nobody listening: skip the path lookups entirely
        dirs = await self.dirs_to_forget(event)
        if dirs:
            log.debug("[rcbridge] %s → forget %s", event.get("op"), dirs)
            await self.broadcast_forget(dirs)

    # -- LISTEN loop -------------------------------------------------------

    def _on_notify(self, _conn, _pid, _channel, payload) -> None:
        # asyncpg invokes this synchronously on the loop: schedule the async work.
        task = asyncio.create_task(self.handle_event(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        self._http = aiohttp.ClientSession()
        backoff = 1.0
        try:
            while True:
                conn = None
                try:
                    conn = await asyncpg.connect(self._dsn)
                    self._reconnect.clear()
                    conn.add_termination_listener(lambda _c: self._reconnect.set())
                    await conn.add_listener("changes", self._on_notify)
                    log.info("[rcbridge] listening on 'changes'")
                    backoff = 1.0
                    await self._reconnect.wait()
                    log.warning("[rcbridge] LISTEN connection lost; reconnecting")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never fatal; retry
                    log.warning("[rcbridge] LISTEN error (%s); retrying in %.0fs", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                finally:
                    if conn is not None:
                        await conn.close()
        except asyncio.CancelledError:
            pass
        finally:
            for t in list(self._tasks):
                t.cancel()
            await self._http.close()
            log.info("[rcbridge] stopped")


async def start_rcbridge(config, *, session_factory, registry: RcRegistry) -> asyncio.Task | None:
    """Start the bridge task, or None if disabled / unusable.

    `bridge_enabled` needs `changes_feed.enabled` (the notify is emitted by the
    feed triggers): otherwise the WebDAV data-plane still works but there is no
    auto-refresh — logged as a clear warning, never fatal.
    """
    if not config.rclone.bridge_enabled:
        return None
    if not config.changes_feed.enabled:
        log.warning(
            "[rcbridge] rclone.bridge_enabled is true but changes_feed.enabled is false; "
            "auto-refresh is OFF (mounts only refresh on --dir-cache-time)"
        )
        return None
    bridge = RcBridge(asyncpg_dsn(config.db), session_factory, registry)
    return asyncio.create_task(bridge.run())
