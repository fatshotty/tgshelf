"""FsExecutor: the global concurrency governor for FS management operations.

Runs up to `operations.concurrent` operations at once. Each operation gets its
OWN AsyncSession (an AsyncSession is not safe for concurrent use) and a
FileSystem bound to a leased USER account — its client is the gateway, so all the
operation's Telegram calls (forward/delete) go through ONE account (a complete
file is never split across accounts). Accounts are leased least-loaded with
wait-on-cooldown (the same pool logic the streamer uses for bots).

Scope: management ops that fan out per-file (move/copy/delete). Uploads keep the
Uploader's own per-file account leasing (the upload concurrency knob lives in the
sync command), so an upload is never double-leased.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Iterable

from tgshelf.core.fs import FileSystem
from tgshelf.db.repo import NodeRepo


class NoAccountAvailable(Exception):
    """No user account can be leased for an operation (all unavailable)."""


class FsExecutor:
    def __init__(
        self,
        session_factory: Callable[[], Any],
        user_pool: Any,
        *,
        master_channel: int,
        concurrent: int = 1,
        min_size: int = 0,
        uploader: Any = None,
        streamer: Any = None,
        gateway: Any = None,
        notifier: Any = None,
        caption_template: str = "fileName: {filename}",
        plugin_manager: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._session_factory = session_factory
        self._user_pool = user_pool
        self._master_channel = master_channel
        self._concurrent = concurrent
        self._min_size = min_size
        self._uploader = uploader
        self._streamer = streamer
        self._gateway = gateway
        self._notifier = notifier
        self._caption_template = caption_template
        self._plugin_manager = plugin_manager
        self._sleep = sleep

    async def run(
        self, items: Iterable[Any], op: Callable[[FileSystem, Any], Awaitable[Any]]
    ) -> list[Any]:
        """Run `op(fs, item)` for each item, max `concurrent` in flight. Per-item
        exceptions are captured (one bad item never aborts the batch)."""
        sem = asyncio.Semaphore(self._concurrent)

        async def worker(item: Any) -> Any:
            async with sem:
                return await self._run_one(item, op)

        return await asyncio.gather(
            *(worker(item) for item in items), return_exceptions=True
        )

    async def _run_one(self, item: Any, op) -> Any:
        member = await self._user_pool.lease_or_wait(sleep=self._sleep)
        if member is None:
            raise NoAccountAvailable("no user account available for the operation")
        self._user_pool.acquire(member)
        try:
            async with self._session_factory() as session:
                fs = FileSystem(
                    NodeRepo(session),
                    master_channel=self._master_channel,
                    gateway=self._gateway or member.client,
                    uploader=self._uploader,
                    streamer=self._streamer,
                    min_size=self._min_size,
                    notifier=self._notifier,
                    caption_template=self._caption_template,
                    plugin_manager=self._plugin_manager,
                )
                return await op(fs, item)
        finally:
            self._user_pool.release(member)
