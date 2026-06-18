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
import copy
import logging
import math
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger("tgshelf.client")

from telethon import errors as tg_errors
from telethon import functions
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
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
        # per-DC media senders, connected to each DC's media_only endpoint and
        # reused for every chunk/stream (the legacy get_media_session, Telethon
        # edition). Downloading on the regular endpoint / main connection makes
        # Telegram throttle sustained GetFile with ~1-2s FloodWaits; the media
        # endpoint does not. Built lazily, disconnected by aclose().
        self._media_senders: dict[int, Any] = {}
        self._media_lock = asyncio.Lock()

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
        # Always read on the DC's MEDIA endpoint, never the regular endpoint /
        # main connection: Telegram throttles sustained GetFile on the regular
        # endpoint with ~1-2s FloodWaits (the legacy used a dedicated is_media
        # session for exactly this reason). One cached media sender per DC is
        # reused for every chunk of every stream.
        dc_id = ref.dc_id if ref.dc_id is not None else getattr(self._client.session, "dc_id", None)
        sender = await self._media_sender(dc_id)
        t_send = time.monotonic()
        try:
            result = await self._with_middleware(lambda: sender.send(request))
        except ConnectionError:
            # the media connection dropped: discard it so the next call rebuilds,
            # and let the streamer retry this chunk (per-bot failover loop).
            self._media_senders.pop(dc_id, None)
            raise
        self._log_slow_fetch(dc_id, "media", borrow=0.0, send=time.monotonic() - t_send, ret=0.0)
        return result.bytes

    async def _media_sender(self, dc_id: int) -> MTProtoSender:
        sender = self._media_senders.get(dc_id)
        if sender is not None:
            return sender
        async with self._media_lock:
            sender = self._media_senders.get(dc_id)
            if sender is None:
                sender = await self._build_media_sender(dc_id)
                self._media_senders[dc_id] = sender
            return sender

    async def _build_media_sender(self, dc_id: int) -> MTProtoSender:
        """A dedicated MTProtoSender to `dc_id`'s media_only endpoint, reused for
        all downloads on that DC. Mirrors the legacy get_media_session: the home
        DC reuses the existing auth_key (no ExportAuthorization, which Telegram
        refuses for the home DC); a foreign DC handshakes a fresh key and imports
        an exported authorization, exactly like Telethon's _create_exported_sender
        — but pointed at the media endpoint instead of the regular one."""
        client = self._client
        cfg = await self._with_middleware(lambda: client(functions.help.GetConfigRequest()))
        opts = [
            o for o in cfg.dc_options
            if o.id == dc_id and not o.cdn and bool(o.ipv6) == client._use_ipv6
        ]
        chosen = next((o for o in opts if o.media_only), None) or (opts[0] if opts else None)
        if chosen is None:
            raise ConnectionError(f"no dc_option for dc {dc_id}")

        home_dc = getattr(client.session, "dc_id", None)
        auth_key = client.session.auth_key if dc_id == home_dc else None
        sender = MTProtoSender(auth_key, loggers=client._log)
        await sender.connect(client._connection(
            chosen.ip_address, chosen.port, dc_id,
            loggers=client._log, proxy=client._proxy, local_addr=client._local_addr,
        ))
        sender.dc_id = dc_id
        # a freshly connected sender must carry initConnection on its first call
        init = copy.copy(client._init_request)
        if dc_id == home_dc:
            init.query = functions.help.GetConfigRequest()
        else:
            auth = await self._with_middleware(
                lambda: client(functions.auth.ExportAuthorizationRequest(dc_id))
            )
            init.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
        await sender.send(functions.InvokeWithLayerRequest(LAYER, init))
        log.info(
            "[media] '%s' media sender for dc %d -> %s (media_only=%s)",
            self.name, dc_id, chosen.ip_address, getattr(chosen, "media_only", None),
        )
        return sender

    async def aclose(self) -> None:
        """Disconnect the cached per-DC media senders. Call before disconnecting
        the main client on shutdown. Safe to call more than once."""
        senders, self._media_senders = self._media_senders, {}
        for dc_id, sender in senders.items():
            try:
                await sender.disconnect()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                log.debug("[media] '%s' error disconnecting dc %d sender", self.name, dc_id)

    def _log_slow_fetch(self, dc_id, path, *, borrow, send, ret, threshold=2.0) -> None:
        """Break down a chunk fetch into borrow/send/return when it is REALLY slow
        (> threshold s), to pinpoint which step stalls (the event loop is NOT
        blocked — see [looplag]). Telegram's ~1s natural FloodWaits are below it,
        so they don't fill the log."""
        total = borrow + send + ret
        if total > threshold:
            log.warning(
                "[fetch-slow] '%s' dc %s (%s) total=%.2fs borrow=%.2fs send=%.2fs return=%.2fs",
                self.name, dc_id, path, total, borrow, send, ret,
            )

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
