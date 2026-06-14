"""TgClient: a Telethon client wrapped in the invoke middleware.

Every raw call goes through `_with_middleware`: a per-client semaphore bounds
concurrent transmissions, short FloodWaits are slept-and-retried inline, long
ones surface as FloodCooldown for the pool, transient server errors get a
bounded exponential backoff, and the rest become typed domain errors. The raw
upload/download/forward methods implement the Gateway protocol so engines never
touch telethon — they are exercised by manual smoke tests (real Telegram).
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Awaitable, Callable

log = logging.getLogger("tgshelf.client")

from telethon import errors as tg_errors
from telethon.tl.functions.messages import SendMediaRequest
from telethon.tl.functions.upload import GetFileRequest, SaveBigFilePartRequest
from telethon.tl.types import (
    DocumentAttributeFilename,
    InputDocument,
    InputDocumentFileLocation,
    InputFileBig,
    InputMediaDocument,
    InputMediaUploadedDocument,
)

from tgshelf.constants import CHUNK_SIZE
from tgshelf.telegram.errors import FloodCooldown, translate_telethon_error
from tgshelf.telegram.gateway import DocRef

# transient server-side failures worth a bounded retry (RpcCallFailError is a
# ServerError subclass, so this covers it too)
_TRANSIENT = (tg_errors.ServerError, tg_errors.TimedOutError)

_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 8.0


class TgClient:
    def __init__(
        self,
        client: Any,
        *,
        name: str,
        semaphore_limit: int = 4,
        flood_threshold: int = 25,
        max_retries: int = 3,
        max_flood_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rate_limiter: Any = None,
    ):
        self._client = client
        self.name = name
        self._sem = asyncio.Semaphore(semaphore_limit)
        self._flood_threshold = flood_threshold
        self._max_retries = max_retries
        self._max_flood_retries = max_flood_retries
        self._sleep = sleep
        self._rate_limiter = rate_limiter

    # -- middleware --------------------------------------------------------

    async def invoke(self, request: Any) -> Any:
        return await self._with_middleware(lambda: self._client(request))

    async def _with_middleware(self, do_call: Callable[[], Awaitable[Any]]) -> Any:
        async with self._sem:
            transient = 0
            floods = 0
            while True:
                # proactive per-account rate limit: bench the account before
                # Telegram floods it (reuses the FloodCooldown failover path)
                if self._rate_limiter is not None:
                    wait = self._rate_limiter.acquire(self.name)
                    if wait > 0:
                        log.debug("[ratelimit] '%s' window full; backing off %.1fs", self.name, wait)
                        raise FloodCooldown(math.ceil(wait))
                try:
                    return await do_call()
                except tg_errors.FloodWaitError as exc:
                    if exc.seconds <= self._flood_threshold and floods < self._max_flood_retries:
                        floods += 1
                        log.debug(
                            "[flood] '%s' FloodWait %ds (<= %ds); sleeping and retrying inline",
                            self.name, exc.seconds, self._flood_threshold,
                        )
                        await self._sleep(exc.seconds)
                        continue
                    log.warning(
                        "[flood] '%s' FloodWait %ds (> %ds); surfacing as cooldown",
                        self.name, exc.seconds, self._flood_threshold,
                    )
                    raise FloodCooldown(exc.seconds) from exc
                except _TRANSIENT as exc:
                    transient += 1
                    if transient >= self._max_retries:
                        log.warning("'%s' transient error, giving up after %d tries: %s",
                                    self.name, transient, exc)
                        raise
                    log.debug("'%s' transient error, retry %d/%d", self.name, transient, self._max_retries)
                    await self._sleep(min(_BACKOFF_BASE * 2 ** (transient - 1), _BACKOFF_CAP))
                    continue
                except Exception as exc:  # noqa: BLE001 - re-raised below
                    domain = translate_telethon_error(exc)
                    if domain is not None:
                        raise domain from exc
                    raise

    # -- gateway: reads ----------------------------------------------------

    async def get_document(self, channel_id: int, message_id: int) -> DocRef | None:
        message = await self._with_middleware(
            lambda: self._client.get_messages(channel_id, ids=message_id)
        )
        if message is None or getattr(message, "empty", False) or not message.media:
            return None

        doc = getattr(message, "document", None)
        if doc is not None:
            return DocRef(
                location=InputDocumentFileLocation(
                    id=doc.id,
                    access_hash=doc.access_hash,
                    file_reference=doc.file_reference,
                    thumb_size="",
                ),
                doc_id=doc.id,
                dc_id=doc.dc_id,
                size=doc.size,
                mime=getattr(doc, "mime_type", None),
                filename=_filename_of(doc),
            )
        # any other media (photo, …) normalized through telethon's input file
        return None

    async def get_file_chunk(self, ref: DocRef, offset: int, limit: int = CHUNK_SIZE) -> bytes:
        request = GetFileRequest(
            location=ref.location, offset=offset, limit=limit, precise=False
        )
        # same DC as the client -> use the main connection directly. Exporting an
        # authorization for the home DC raises DcIdInvalidError (legacy did the
        # same check). Only foreign-DC files need a borrowed media sender.
        home_dc = getattr(self._client.session, "dc_id", None)
        if ref.dc_id is None or ref.dc_id == home_dc:
            result = await self._with_middleware(lambda: self._client(request))
            return result.bytes

        sender = await self._client._borrow_exported_sender(ref.dc_id)
        try:
            result = await self._with_middleware(lambda: sender.send(request))
            return result.bytes
        finally:
            await self._client._return_exported_sender(sender)

    # -- gateway: writes ---------------------------------------------------

    async def save_big_part(
        self, file_id: int, part_idx: int, total_parts: int, data: bytes
    ) -> None:
        await self.invoke(
            SaveBigFilePartRequest(
                file_id=file_id, file_part=part_idx, file_total_parts=total_parts, bytes=data
            )
        )

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
        peer = await self._client.get_input_entity(channel_id)
        media = InputMediaUploadedDocument(
            file=InputFileBig(id=file_id, parts=total_parts, name=filename),
            mime_type=mime,
            attributes=[DocumentAttributeFilename(file_name=filename)],
            force_file=True,
        )
        result = await self.invoke(
            SendMediaRequest(
                peer=peer,
                media=media,
                random_id=_random_id(),
                silent=True,
                message=caption,
            )
        )
        return _extract_sent(result)

    async def copy_message(
        self, from_channel_id: int, message_id: int, to_channel_id: int
    ) -> tuple[int, int]:
        # fetch the source message once: we need both its document (to re-send
        # server-side, no byte transfer) and its caption (the "fileName: …" set
        # at upload), which must be preserved across the move/copy.
        message = await self._with_middleware(
            lambda: self._client.get_messages(from_channel_id, ids=message_id)
        )
        if message is None or getattr(message, "empty", False):
            raise ValueError(f"no message {message_id} in {from_channel_id}")
        doc = getattr(message, "document", None)
        if doc is None:
            raise ValueError(f"message {message_id} of {from_channel_id} has no document")

        peer = await self._client.get_input_entity(to_channel_id)
        media = InputMediaDocument(
            id=InputDocument(
                id=doc.id, access_hash=doc.access_hash, file_reference=doc.file_reference
            ),
            spoiler=False,
        )
        result = await self.invoke(
            SendMediaRequest(
                peer=peer,
                media=media,
                random_id=_random_id(),
                silent=True,
                message=message.message or "",  # preserve the original caption
            )
        )
        return _extract_sent(result)

    async def delete_message(self, channel_id: int, message_id: int) -> bool:
        entity = await self._client.get_input_entity(channel_id)
        result = await self._with_middleware(
            lambda: self._client.delete_messages(entity, [message_id])
        )
        # telethon returns AffectedMessages; pts_count 0 = nothing deleted
        affected = result[0] if isinstance(result, list) else result
        return getattr(affected, "pts_count", 1) > 0


def _filename_of(doc: Any) -> str | None:
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return None


def _random_id() -> int:
    from telethon import helpers

    return helpers.generate_random_long()


def _extract_sent(result: Any) -> tuple[int, int]:
    """Pull (message_id, doc_id) out of an Updates result (mirrors the legacy
    duck-typed scan of SendMedia updates)."""
    for update in getattr(result, "updates", []):
        message = getattr(update, "message", None)
        if message is None:
            continue
        doc = getattr(getattr(message, "media", None), "document", None)
        doc_id = doc.id if doc is not None else None
        if doc_id is not None:
            return message.id, doc_id
    raise ValueError("could not extract message_id/doc_id from SendMedia result")
