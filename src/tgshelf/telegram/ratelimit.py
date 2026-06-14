"""Per-account proactive rate limiting.

A sliding window of "N Telegram API calls per M seconds" per account. The count
point is TgClient.invoke (the single choke point of every raw call): when the
window is full the client cools the account down BEFORE Telegram floods it,
reusing the existing cooldown/failover machinery.

The backend is pluggable behind `RateLimiter`: `InMemoryRateLimiter` (per
instance) now; a shared backend (Redis, keyed on Postgres now()) can be added
later for strict cross-instance budgeting without touching callers.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class RateLimiter(Protocol):
    def acquire(self, account: str) -> float:
        """Record one call for `account`. Return 0.0 if allowed, else the
        seconds until a slot frees (the caller should cool the account down)."""
        ...


class InMemoryRateLimiter:
    def __init__(
        self,
        *,
        max_calls: int,
        window: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max = max_calls
        self._window = window
        self._clock = clock
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def acquire(self, account: str) -> float:
        if self._max <= 0:  # disabled
            return 0.0
        now = self._clock()
        q = self._calls[account]
        cutoff = now - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) < self._max:
            q.append(now)
            return 0.0
        # full: wait until the oldest call falls out of the window
        return q[0] + self._window - now
