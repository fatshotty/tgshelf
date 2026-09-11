"""Client pools: health-aware, weighted-least-loaded leasing.

A Pool wraps a set of clients (user accounts or bots) and decides which to hand
out. Selection is least-loaded first (in_flight / capacity), ties broken by
least-recently-used so requests rotate naturally across the whole pool
(7 bots, k=3 → 1,2,3 / 4,5,6 / 7,1,2 / 3,4,5 / 6,7,1).

Health, all driven by the engines reacting to TgClient errors:
- `mark_flood` puts a client on cooldown until a wall-clock deadline (FloodWait);
- `mark_error`/`mark_success` track a consecutive-error streak; N in a row
  opens a timed quarantine. One real request probes the client after the
  deadline; success recovers it and failure advances the backoff;
- `mark_ineligible` records per-channel access loss (ChannelPrivateError) so a
  bot is skipped only for the channels it cannot reach.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Callable, Iterable, Sequence

log = logging.getLogger("tgshelf.pool")


class PoolMember:
    def __init__(self, client: Any, name: str, *, capacity: int = 4):
        self.client = client
        self.name = name
        self.capacity = capacity
        self.in_flight = 0
        self.consecutive_errors = 0
        self.quarantined = False
        self.half_open = False
        self.quarantine_attempt = 0
        self.quarantine_until = 0.0
        self.cooldown_until = 0.0
        self.last_lease_seq = 0
        self.ineligible_channels: set[int] = set()
        self.is_premium = False  # account tier (user accounts; set at login/recheck)
        self.max_upload_parts = 0

    @property
    def load(self) -> float:
        return self.in_flight / self.capacity if self.capacity else float("inf")


class Pool:
    def __init__(
        self,
        members: Iterable[PoolMember],
        *,
        max_errors: int = 3,
        quarantine_backoff: Sequence[float] = (300.0, 900.0, 1800.0, 3600.0),
        clock: Callable[[], float] = time.monotonic,
    ):
        self._members = list(members)
        self._max_errors = max_errors
        self._quarantine_backoff = tuple(quarantine_backoff)
        if not self._quarantine_backoff or any(
            not math.isfinite(seconds) or seconds <= 0
            for seconds in self._quarantine_backoff
        ):
            raise ValueError("quarantine_backoff must contain finite positive durations")
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

    def _lease_due_probe(
        self,
        channel_id: int | None,
        exclude: Sequence[PoolMember] = (),
    ) -> PoolMember | None:
        if any(member.half_open for member in self._members):
            return None
        now = self._clock()
        excluded = {id(member) for member in exclude}
        due = [
            member
            for member in self._members
            if member.quarantined
            and not member.half_open
            and member.quarantine_until <= now
            and member.cooldown_until <= now
            and member.in_flight == 0
            and id(member) not in excluded
            and (channel_id is None or channel_id not in member.ineligible_channels)
        ]
        if not due:
            return None
        member = min(due, key=lambda item: (item.quarantine_until, item.last_lease_seq))
        member.half_open = True
        self._touch(member)
        log.info(
            "[probe] '%s' quarantine expired; leasing one half-open request",
            member.name,
        )
        return member

    def lease(
        self,
        k: int,
        channel_id: int | None = None,
        *,
        allow_probe: bool = False,
    ) -> list[PoolMember]:
        if k <= 0:
            return []
        chosen: list[PoolMember] = []
        probe = self._lease_due_probe(channel_id) if allow_probe else None
        if probe is not None:
            chosen.append(probe)
        healthy = self._ranked(channel_id)[: k - len(chosen)]
        for member in healthy:
            self._touch(member)
        chosen.extend(healthy)
        return chosen

    def replace(
        self,
        channel_id: int | None = None,
        exclude: Sequence[PoolMember] = (),
        *,
        allow_probe: bool = False,
    ) -> PoolMember | None:
        probe = self._lease_due_probe(channel_id, exclude) if allow_probe else None
        if probe is not None:
            return probe
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
        allow_probe: bool = False,
        sleep=asyncio.sleep,  # awaited below: must be an async sleep, not time.sleep
    ) -> PoolMember | None:
        """Lease the least-loaded eligible member; if all eligible ones are on
        cooldown, wait for the earliest to free and retry. Returns None when no
        member can become available without a later request probing quarantine.

        `exclude` is honored only for the immediate attempt — after waiting, an
        excluded member whose cooldown has expired is fair game again. Reused by
        the streamer (bots) and the operation executor (user accounts)."""
        member = self.replace(
            channel_id=channel_id, exclude=exclude, allow_probe=allow_probe
        )
        if member is not None:
            return member
        while True:
            wait = self._earliest_cooldown_wait(channel_id)
            quarantine_wait = (
                self.next_quarantine_wait(channel_id) if allow_probe else None
            )
            if wait is None or (
                quarantine_wait is not None and quarantine_wait < wait
            ):
                if quarantine_wait is None:
                    log.warning(
                        "[exhausted] no client can serve channel %s right now",
                        channel_id,
                    )
                else:
                    log.warning(
                        "[exhausted] no client can serve channel %s; next probe in %.1fs",
                        channel_id,
                        quarantine_wait,
                    )
                return None
            log.info("[wait] all clients busy/cooled; waiting %.1fs for the next free", wait)
            await sleep(max(wait, 0))
            member = self.replace(
                channel_id=channel_id, allow_probe=allow_probe
            )
            if member is not None:
                log.debug("[wait] '%s' became available", member.name)
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

    def next_quarantine_wait(self, channel_id: int | None = None) -> float | None:
        """Return seconds until the next eligible quarantine probe.

        Requests are not held open for quarantine backoffs. A request arriving
        once this reaches zero becomes the single half-open probe.
        """
        now = self._clock()
        waits = [
            max(0.0, max(member.quarantine_until, member.cooldown_until) - now)
            for member in self._members
            if member.quarantined
            and not member.half_open
            and (channel_id is None or channel_id not in member.ineligible_channels)
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
        log.warning("[flood] '%s' PAUSED for %.0fs (cooldown)", m.name, seconds)

    def abort_probe(self, m: PoolMember) -> None:
        if not m.half_open:
            return
        m.half_open = False
        m.quarantine_until = self._clock()
        log.info(
            "[probe] '%s' half-open request aborted; remains quarantined", m.name
        )

    def mark_error(self, m: PoolMember) -> None:
        if m.quarantined:
            if not m.half_open:
                log.debug("[quarantine] '%s' ignored an in-flight error", m.name)
                return
            m.half_open = False
            m.quarantine_attempt += 1
            delay = self._quarantine_delay(m.quarantine_attempt)
            m.quarantine_until = self._clock() + delay
            log.warning(
                "[quarantine] '%s' half-open probe failed; attempt=%d retry_in=%.0fs",
                m.name,
                m.quarantine_attempt,
                delay,
            )
            return

        m.consecutive_errors += 1
        if m.consecutive_errors >= self._max_errors:
            m.quarantined = True
            m.half_open = False
            m.quarantine_attempt = 1
            delay = self._quarantine_delay(m.quarantine_attempt)
            m.quarantine_until = self._clock() + delay
            log.warning(
                "[quarantine] '%s' QUARANTINED after %d consecutive errors; "
                "attempt=%d retry_in=%.0fs",
                m.name,
                m.consecutive_errors,
                m.quarantine_attempt,
                delay,
            )
        else:
            log.debug("'%s' error %d/%d", m.name, m.consecutive_errors, self._max_errors)

    def mark_success(self, m: PoolMember) -> None:
        if m.half_open:
            self.recover(m)
            return
        if m.quarantined:
            return
        if m.consecutive_errors:
            log.debug("'%s' recovered after %d error(s)", m.name, m.consecutive_errors)
        m.consecutive_errors = 0

    def _quarantine_delay(self, attempt: int) -> float:
        index = min(max(attempt - 1, 0), len(self._quarantine_backoff) - 1)
        return self._quarantine_backoff[index]

    def mark_ineligible(self, m: PoolMember, channel_id: int) -> None:
        m.ineligible_channels.add(channel_id)
        log.warning("[eligibility] '%s' cannot reach channel %s (excluded for it)", m.name, channel_id)

    def recover(self, m: PoolMember) -> None:
        was = m.quarantined
        m.quarantined = False
        m.half_open = False
        m.quarantine_attempt = 0
        m.quarantine_until = 0.0
        m.consecutive_errors = 0
        if was:
            log.info("[recover] '%s' quarantine lifted", m.name)


class ClientPool(Pool):
    """User accounts: upload and bulk management."""

    def lease_one(self) -> PoolMember | None:
        leased = self.lease(1)
        return leased[0] if leased else None


class BotPool(Pool):
    """Bots: download and streaming, leased per channel."""

    def lease_bots(self, channel_id: int, k: int) -> list[PoolMember]:
        return self.lease(k, channel_id=channel_id, allow_probe=True)
