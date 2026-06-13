"""Upload coordinator: account selection + per-account boundary + premium defense.

Wraps UploadEngine with the policy that can't live in the pure engine:

- uploads run on a USER account only (leased from the ClientPool — bots are
  download/stream and are never asked to upload);
- the portion boundary (max parts) is a property of the LEASED account: premium
  ~4GB / free ~2GB, deduced at login and re-checkable at upload start;
- premium-expired defense: if Telegram rejects parts beyond the free limit at
  runtime (UploadLimitExceeded), the partial portions are cleaned up, the
  account is marked free, the event is notified, and the upload is retried
  automatically with the free boundary — the user is warned but the file arrives.

The source is a FACTORY (re-openable): the premium retry re-reads from the start.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable

from tgshelf.constants import PART_SIZE
from tgshelf.core.upload import PartRecord, UploadEngine, UploadResult, _default_file_id
from tgshelf.telegram.errors import UploadLimitExceeded
from tgshelf.telegram.pool import ClientPool, PoolMember

SourceFactory = Callable[[], AsyncIterator[bytes]]
Hook = Callable[..., Awaitable[None] | None]


class NoUploadAccount(Exception):
    """No user account is available to perform the upload."""


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value):
        await value


class Uploader:
    def __init__(
        self,
        client_pool: ClientPool,
        *,
        free_max_parts: int = 4000,
        premium_max_parts: int = 8000,
        part_size: int = PART_SIZE,
        max_in_flight: int = 3,
        premium_check: Callable[[Any], Awaitable[bool]] | None = None,
        notifier: Callable[[Exception], Any] | None = None,
        file_id_factory: Callable[[], int] = _default_file_id,
    ):
        self._pool = client_pool
        self._free_max = free_max_parts
        self._premium_max = premium_max_parts
        self._part_size = part_size
        self._max_in_flight = max_in_flight
        self._premium_check = premium_check
        self._notifier = notifier
        self._file_id_factory = file_id_factory

    async def upload(
        self,
        source_factory: SourceFactory,
        *,
        filename: str,
        mime: str,
        channel_id: int,
        min_size: int,
        on_part: Hook | None = None,
        on_reset: Callable[[], Any] | None = None,
    ) -> UploadResult:
        member = self._pool.lease_one()
        if member is None:
            raise NoUploadAccount("no user account available for upload")

        self._pool.acquire(member)
        try:
            if self._premium_check is not None:
                member.is_premium = await self._premium_check(member.client)
            try:
                return await self._attempt(
                    member, source_factory, filename, mime, channel_id,
                    min_size, on_part, is_premium=member.is_premium,
                )
            except UploadLimitExceeded as exc:
                # premium expired at runtime: downgrade, warn, reset, retry free
                member.is_premium = False
                if self._notifier is not None:
                    self._notifier(exc)
                if on_reset is not None:
                    await _maybe_await(on_reset())
                return await self._attempt(
                    member, source_factory, filename, mime, channel_id,
                    min_size, on_part, is_premium=False,
                )
        finally:
            self._pool.release(member)

    async def _attempt(
        self, member, source_factory, filename, mime, channel_id, min_size, on_part, *, is_premium
    ) -> UploadResult:
        boundary = self._premium_max if is_premium else self._free_max
        engine = UploadEngine(
            member.client,
            part_size=self._part_size,
            max_in_flight=self._max_in_flight,
            file_id_factory=self._file_id_factory,
        )
        sent: list[PartRecord] = []

        async def track(rec: PartRecord) -> None:
            sent.append(rec)
            if on_part is not None:
                await _maybe_await(on_part(rec))

        try:
            return await engine.upload(
                source_factory(),
                filename=filename,
                mime=mime,
                channel_id=channel_id,
                min_size=min_size,
                max_upload_parts=boundary,
                on_part=track,
            )
        except UploadLimitExceeded:
            # delete any portions already finalized into the channel
            for rec in sent:
                await member.client.delete_message(rec.channel_id, rec.message_id)
            raise
