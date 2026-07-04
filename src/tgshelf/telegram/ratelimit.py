"""Per-account proactive Telegram write budgeting.

The limiter is a token bucket per account. Each flood-sensitive Telegram write
consumes one token. Buckets refill gradually over `refill_seconds`, allowing a
short burst while preventing sustained write pressure from concentrating on one
account. Reads, downloads, and SaveBigFilePart upload chunks are not proactively
limited; they still handle Telegram's real FloodWait responses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class RateLimiter(Protocol):
    def acquire(self, account: str) -> float:
        """Try to reserve one write for `account`.

        Return 0.0 when a token was reserved. Return the seconds until the next
        token is available when the account is currently over budget.
        """
        ...


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketRateLimiter:
    def __init__(
        self,
        *,
        capacity: int,
        refill_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._capacity = capacity
        self._refill_seconds = refill_seconds
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    @property
    def enabled(self) -> bool:
        return self._capacity > 0

    def acquire(self, account: str) -> float:
        if not self.enabled:
            return 0.0
        bucket = self._bucket(account)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return self.available_in(account)

    def available_in(self, account: str) -> float:
        if not self.enabled:
            return 0.0
        bucket = self._bucket(account)
        if bucket.tokens >= 1.0:
            return 0.0
        return (1.0 - bucket.tokens) / self._refill_rate

    @property
    def _refill_rate(self) -> float:
        return self._capacity / self._refill_seconds

    def _bucket(self, account: str) -> _Bucket:
        now = self._clock()
        bucket = self._buckets.get(account)
        if bucket is None:
            bucket = _Bucket(tokens=float(self._capacity), updated_at=now)
            self._buckets[account] = bucket
            return bucket
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(
            float(self._capacity),
            bucket.tokens + elapsed * self._refill_rate,
        )
        bucket.updated_at = now
        return bucket
