"""Upload engine: stream → inline content or Telegram portions.

A source byte stream is buffered up to `min_size`: if it ends within the
threshold the body is stored inline (in the DB, never on Telegram); otherwise it
is chunked into fixed `part_size` pieces uploaded with SaveBigFilePart (up to K
in flight — the API accepts out-of-order parts, the main perf win over the
sequential legacy loop), and split into "portions" of at most `max_upload_parts`
parts each. Every portion is finalized with SendMedia into the channel and its
DB row is persisted immediately (crash-safe) via the `on_part` callback.

Portion filenames get a 1-based `.NNN` suffix when the file spans more than one
portion; the decision is explicit (multi = not the only portion), not dependent
on the legacy's create-before-send ordering.

NOTE: uploads run on USER accounts only (bots are download/stream); account
selection and the dynamic per-account size boundary live in the caller (A6.2).
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from tgshelf.constants import PART_SIZE


@dataclass(frozen=True)
class PartRecord:
    idx: int            # portion index (0-based) == parts.idx in the DB
    channel_id: int
    message_id: int
    doc_id: int
    size: int
    original_filename: str


@dataclass(frozen=True)
class UploadResult:
    size: int
    inline_content: bytes | None = None
    parts: tuple[PartRecord, ...] = ()


def _default_file_id() -> int:
    # positive, < 2^63 (Telegram int64); mirrors the legacy "18 digits + 1" guard
    return secrets.randbits(62) + 1


class UploadEngine:
    def __init__(
        self,
        gateway: Any,
        *,
        part_size: int = PART_SIZE,
        max_in_flight: int = 3,
        file_id_factory: Callable[[], int] = _default_file_id,
    ):
        self._gw = gateway
        self._part_size = part_size
        self._max_in_flight = max_in_flight
        self._new_file_id = file_id_factory

    async def upload(
        self,
        source: AsyncIterator[bytes],
        *,
        filename: str,
        mime: str,
        channel_id: int,
        min_size: int,
        max_upload_parts: int,
        on_part: Callable[[PartRecord], Awaitable[None] | None] | None = None,
    ) -> UploadResult:
        stream = aiter(source)

        # buffer until we exceed min_size or hit EOF
        buf = bytearray()
        eof = False
        while len(buf) <= min_size:
            try:
                buf.extend(await anext(stream))
            except StopAsyncIteration:
                eof = True
                break
        if eof and len(buf) <= min_size:
            return UploadResult(size=len(buf), inline_content=bytes(buf))

        return await self._upload_portions(
            buf, stream, filename, mime, channel_id, max_upload_parts, on_part
        )

    async def _upload_portions(
        self, buf, stream, filename, mime, channel_id, max_upload_parts, on_part
    ) -> UploadResult:
        records: list[PartRecord] = []
        sem = asyncio.Semaphore(self._max_in_flight)
        in_flight: list[asyncio.Task] = []

        file_id = self._new_file_id()
        part_idx = 0          # SaveBigFilePart index within the current portion
        portion_idx = 0
        portion_size = 0

        pieces = self._pieces(buf, stream)
        prev = await anext(pieces, None)
        while prev is not None:
            nxt = await anext(pieces, None)
            is_last_piece = nxt is None

            await sem.acquire()  # gate reading: bounds pieces in memory to K
            in_flight.append(
                asyncio.create_task(self._save(file_id, part_idx, prev, sem))
            )
            part_idx += 1
            portion_size += len(prev)

            if part_idx >= max_upload_parts or is_last_piece:
                await asyncio.gather(*in_flight)  # surfaces save errors too
                in_flight.clear()
                record = await self._finalize(
                    file_id, portion_idx, part_idx, portion_size,
                    filename, mime, channel_id, is_last_portion=is_last_piece,
                )
                records.append(record)
                if on_part is not None:
                    result = on_part(record)
                    if asyncio.iscoroutine(result):
                        await result
                portion_idx += 1
                if not is_last_piece:
                    file_id = self._new_file_id()
                    part_idx = 0
                    portion_size = 0
            prev = nxt

        return UploadResult(
            size=sum(r.size for r in records), parts=tuple(records)
        )

    async def _pieces(self, buf, stream) -> AsyncIterator[bytes]:
        """Re-chunk the already-read buffer plus the rest of the stream into
        exact `part_size` pieces (last one may be shorter)."""
        carry = bytearray(buf)
        size = self._part_size
        while len(carry) >= size:
            yield bytes(carry[:size])
            del carry[:size]
        async for chunk in stream:
            carry.extend(chunk)
            while len(carry) >= size:
                yield bytes(carry[:size])
                del carry[:size]
        if carry:
            yield bytes(carry)

    async def _save(self, file_id, part_idx, data, sem):
        try:
            await self._gw.save_big_part(file_id, part_idx, -1, data)
        finally:
            sem.release()

    async def _finalize(
        self, file_id, portion_idx, total_parts, size, filename, mime, channel_id, *, is_last_portion
    ) -> PartRecord:
        multi = portion_idx > 0 or not is_last_portion
        name = f"{filename}.{portion_idx + 1:03d}" if multi else filename
        message_id, doc_id = await self._gw.send_document(
            channel_id, file_id, total_parts, name, size, mime, f"fileName: {name}"
        )
        return PartRecord(
            idx=portion_idx,
            channel_id=channel_id,
            message_id=message_id,
            doc_id=doc_id,
            size=size,
            original_filename=name,
        )
