"""Client pools: health-aware, weighted-least-loaded leasing.

A Pool wraps a set of clients (user accounts or bots) and decides which to hand
out. Selection is least-loaded first (in_flight / capacity), ties broken by
least-recently-used so requests rotate naturally across the whole pool
(7 bots, k=3 → 1,2,3 / 4,5,6 / 7,1,2 / 3,4,5 / 6,7,1).

Health, all driven by the engines reacting to TgClient errors:
- `mark_flood` puts a client on cooldown until a wall-clock deadline (FloodWait);
- `mark_error`/`mark_success` track a consecutive-error streak; N in a row
  quarantines the client until `recover` (e.g. `tgshelf bots check`);
- `mark_ineligible` records per-channel access loss (ChannelPrivateError) so a
  bot is skipped only for the channels it cannot reach.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Sequence


class PoolMember:
    def __init__(self, client: Any, name: str, *, capacity: int = 4):
        self.client = client
        self.name = name
        self.capacity = capacity
        self.in_flight = 0
        self.consecutive_errors = 0
        self.quarantined = False
        self.cooldown_until = 0.0
        self.last_lease_seq = 0
        self.ineligible_channels: set[int] = set()
        self.is_premium = False  # account tier (user accounts; set at login/recheck)

    @property
    def load(self) -> float:
        return self.in_flight / self.capacity if self.capacity else float("inf")


class Pool:
    def __init__(
        self,
        members: Iterable[PoolMember],
        *,
        max_errors: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._members = list(members)
        self._max_errors = max_errors
        self._clock = clock
        self._seq = 0

    @property
    def members(self) -> list[PoolMember]:
        return list(self._members)

    def now(self) -> float:
        return self._clock()

    # -- availability ------------------------------------------------------

    def _is_available(self, m: PoolMember, channel_id: int | None, now: float) -> bool:
        if m.quarantined:
            return False
        if m.cooldown_until > now:
            return False
        if channel_id is not None and channel_id in m.ineligible_channels:
            return False
        return True

    def available(self, channel_id: int | None = None) -> list[PoolMember]:
        now = self._clock()
        return [m for m in self._members if self._is_available(m, channel_id, now)]

    # -- leasing -----------------------------------------------------------

    def _ranked(self, channel_id: int | None) -> list[PoolMember]:
        # least loaded first, ties broken by least-recently-used
        return sorted(self.available(channel_id), key=lambda m: (m.load, m.last_lease_seq))

    def _touch(self, m: PoolMember) -> None:
        self._seq += 1
        m.last_lease_seq = self._seq

    def lease(self, k: int, channel_id: int | None = None) -> list[PoolMember]:
        chosen = self._ranked(channel_id)[:k]
        for m in chosen:
            self._touch(m)
        return chosen

    def replace(
        self, channel_id: int | None = None, exclude: Sequence[PoolMember] = ()
    ) -> PoolMember | None:
        excluded = {id(m) for m in exclude}
        for m in self._ranked(channel_id):
            if id(m) not in excluded:
                self._touch(m)
                return m
        return None

    async def lease_or_wait(
        self,
        *,
        channel_id: int | None = None,
        exclude: Sequence[PoolMember] = (),
        sleep=time.sleep,
    ) -> PoolMember | None:
        """Lease the least-loaded eligible member; if all eligible ones are on
        cooldown, wait for the earliest to free and retry. Returns None when no
        member can ever become available (all quarantined/ineligible).

        `exclude` is honored only for the immediate attempt — after waiting, an
        excluded member whose cooldown has expired is fair game again. Reused by
        the streamer (bots) and the operation executor (user accounts)."""
        member = self.replace(channel_id=channel_id, exclude=exclude)
        if member is not None:
            return member
        while True:
            wait = self._earliest_cooldown_wait(channel_id)
            if wait is None:
                return None  # nothing will free up on its own
            await sleep(max(wait, 0))
            member = self.replace(channel_id=channel_id)
            if member is not None:
                return member

    def _earliest_cooldown_wait(self, channel_id: int | None) -> float | None:
        now = self._clock()
        waits = [
            m.cooldown_until - now
            for m in self._members
            if not m.quarantined
            and (channel_id is None or channel_id not in m.ineligible_channels)
            and m.cooldown_until > now
        ]
        return min(waits) if waits else None

    # -- in-flight bookkeeping (for load balancing, not concurrency: the
    #    real GetFile concurrency cap lives in TgClient's semaphore) --------

    def acquire(self, m: PoolMember) -> None:
        m.in_flight += 1

    def release(self, m: PoolMember) -> None:
        m.in_flight = max(0, m.in_flight - 1)

    # -- health ------------------------------------------------------------

    def mark_flood(self, m: PoolMember, seconds: float) -> None:
        m.cooldown_until = self._clock() + seconds

    def mark_error(self, m: PoolMember) -> None:
        m.consecutive_errors += 1
        if m.consecutive_errors >= self._max_errors:
            m.quarantined = True

    def mark_success(self, m: PoolMember) -> None:
        m.consecutive_errors = 0

    def mark_ineligible(self, m: PoolMember, channel_id: int) -> None:
        m.ineligible_channels.add(channel_id)

    def recover(self, m: PoolMember) -> None:
        m.quarantined = False
        m.consecutive_errors = 0


class ClientPool(Pool):
    """User accounts: upload and bulk management."""

    def lease_one(self) -> PoolMember | None:
        leased = self.lease(1)
        return leased[0] if leased else None


class BotPool(Pool):
    """Bots: download and streaming, leased per channel."""

    def lease_bots(self, channel_id: int, k: int) -> list[PoolMember]:
        return self.lease(k, channel_id=channel_id)
