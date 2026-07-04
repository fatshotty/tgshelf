"""Write-aware gateway over the user account pool.

Management operations should not pin all Telegram writes to the account that
happened to run the surrounding filesystem job. This gateway chooses an eligible
user account per write, using the proactive token bucket before the call and the
pool's real FloodWait cooldowns after Telegram errors.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from tgshelf.telegram.errors import ChannelUnavailable, FloodCooldown
from tgshelf.telegram.pool import ClientPool, PoolMember

log = logging.getLogger("tgshelf.write")


class NoWriteAccountAvailable(Exception):
    """No user account can currently perform a Telegram write."""


class AccountWriteGateway:
    def __init__(
        self,
        pool: ClientPool,
        *,
        limiter: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._pool = pool
        self._limiter = limiter
        self._sleep = sleep

    async def get_document(self, channel_id: int, message_id: int):
        member = await self._lease_read()
        try:
            return await member.client.get_document(channel_id, message_id)
        finally:
            self._pool.release(member)

    async def send_document(
        self,
        channel_id: int,
        file_id: int,
        total_parts: int,
        filename: str,
        size: int,
        mime: str,
        caption: str,
    ) -> tuple[int, int]:
        return await self._write(
            lambda member: member.client.send_document(
                channel_id,
                file_id,
                total_parts,
                filename,
                size,
                mime,
                caption,
                rate_limit=False,
            )
        )

    async def copy_message(
        self,
        from_channel_id: int,
        message_id: int,
        to_channel_id: int,
        *,
        caption: str | None = None,
    ) -> tuple[int, int]:
        return await self._write(
            lambda member: member.client.copy_message(
                from_channel_id,
                message_id,
                to_channel_id,
                caption=caption,
                rate_limit=False,
            )
        )

    async def delete_message(self, channel_id: int, message_id: int) -> bool:
        return await self._write(
            lambda member: member.client.delete_message(
                channel_id,
                message_id,
                rate_limit=False,
            )
        )

    async def edit_message_caption(
        self, channel_id: int, message_id: int, caption: str
    ) -> None:
        await self._write(
            lambda member: member.client.edit_message_caption(
                channel_id,
                message_id,
                caption,
                rate_limit=False,
            )
        )

    async def _lease_read(self) -> PoolMember:
        member = await self._pool.lease_or_wait(sleep=self._sleep)
        if member is None:
            raise NoWriteAccountAvailable("no user account available for Telegram reads")
        self._pool.acquire(member)
        return member

    async def _write(self, call: Callable[[PoolMember], Awaitable[Any]]) -> Any:
        while True:
            member = await self._reserve_write_member()
            try:
                return await call(member)
            except FloodCooldown as exc:
                self._pool.mark_flood(member, exc.seconds)
                log.debug("[ratelimit] '%s' hit Telegram FloodWait after reservation", member.name)
            except ChannelUnavailable:
                raise
            finally:
                self._pool.release(member)

    async def _reserve_write_member(self) -> PoolMember:
        while True:
            waits: list[float] = []
            for member in self._pool._ranked(None):  # same ranking policy as normal leases
                wait = self._reserve_token(member.name)
                if wait <= 0:
                    self._pool._touch(member)
                    self._pool.acquire(member)
                    return member
                waits.append(wait)

            if waits:
                cooldown_wait = self._pool._earliest_cooldown_wait(None)
                wait = min(
                    [*waits, cooldown_wait]
                    if cooldown_wait is not None
                    else waits
                )
                log.debug("[ratelimit] all user write buckets full; waiting %.1fs", wait)
                await self._sleep(wait)
                continue

            member = await self._pool.lease_or_wait(sleep=self._sleep)
            if member is None:
                raise NoWriteAccountAvailable("no user account available for Telegram writes")
            self._pool.release(member)

    def _reserve_token(self, account: str) -> float:
        if self._limiter is None:
            return 0.0
        return float(self._limiter.acquire(account))
