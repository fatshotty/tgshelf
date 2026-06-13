"""Throttling for massive Telegram operations (move/copy/delete of subtrees).

Runs many async ops in chunks of `batch`, each chunk with at most `concurrent`
in flight, pausing `sleep` seconds between chunks — the empirically-tuned recipe
to avoid FloodWait on bulk operations (legacy `operations.concurrent/sleep/batch`).
Per-item exceptions are captured (one bad item never aborts the run); the caller
inspects the results and logs/notifies failures.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Iterable


class Throttle:
    def __init__(
        self,
        *,
        concurrent: int = 1,
        batch: int = 10,
        sleep: float = 1.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.concurrent = concurrent
        self.batch = batch
        self.sleep = sleep
        self._sleeper = sleeper

    @classmethod
    def from_config(cls, operations: Any, **kw) -> "Throttle":
        return cls(
            concurrent=operations.concurrent,
            batch=operations.batch,
            sleep=operations.sleep,
            **kw,
        )

    async def run(
        self, items: Iterable[Any], op: Callable[[Any], Awaitable[Any]]
    ) -> list[Any]:
        items = list(items)
        results: list[Any] = []
        sem = asyncio.Semaphore(self.concurrent)

        async def run_one(item: Any) -> Any:
            async with sem:
                return await op(item)

        for start in range(0, len(items), self.batch):
            chunk = items[start : start + self.batch]
            results.extend(
                await asyncio.gather(
                    *(run_one(item) for item in chunk), return_exceptions=True
                )
            )
            if start + self.batch < len(items):  # pause between chunks, not after last
                await self._sleeper(self.sleep)
        return results
