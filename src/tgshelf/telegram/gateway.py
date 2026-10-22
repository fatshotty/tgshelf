"""Gateway protocol: the ONLY Telegram surface the engines see.

Engines (download/upload/channels/fs) depend on this Protocol and on DocRef —
never on telethon types. TgClient implements it for real; tests provide a
FakeGateway. `location` inside DocRef is deliberately opaque: it is produced
and consumed by the gateway implementation only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class DocRef:
    """Normalized reference to ANY media of a message (document, photo, video,
    audio…): the engines treat every file as a generic document."""

    location: Any  # opaque (telethon InputLocation), gateway-internal
    doc_id: int | None
    dc_id: int
    size: int
    mime: str | None = None
    filename: str | None = None
    caption: str | None = None


@dataclass(frozen=True)
class HistoryEntry:
    """Account-independent metadata from one channel message."""
    message_id: int
    date: datetime
    filename: str | None
    mime: str | None
    doc_id: int | None
    size: int
    media_kind: Literal['document', 'unsupported', 'none']


@dataclass(frozen=True)
class HistoryPage:
    entries: tuple[HistoryEntry, ...]
    scanned_through: int
    exhausted: bool


@runtime_checkable
class Gateway(Protocol):
    async def latest_history_id(self, channel_id: int) -> int:
        """Latest accessible message ID, or zero for an empty channel; import-only."""
        ...

    async def history_page(self, channel_id: int, *, after_id: int,
                           through_id: int, limit: int) -> HistoryPage:
        """Oldest messages after the lower bound; imports opt into account pacing."""
        ...

    async def get_document(self, channel_id: int, message_id: int) -> DocRef | None:
        """Fresh DocRef for the media of a message; None if the message is
        gone/empty (deleted from the channel)."""
        ...

    async def get_file_chunk(self, ref: DocRef, offset: int, limit: int) -> bytes:
        """Raw GetFile: offset MUST be a multiple of `limit` (1MiB chunks)."""
        ...

    async def save_big_part(
        self, file_id: int, part_idx: int, total_parts: int, data: bytes
    ) -> None:
        """SaveBigFilePart; total_parts=-1 for streamed intermediate parts, the
        real count on the last part of a portion so Telegram exempts a short
        tail from the part-size rule (else FILE_PART_SIZE_INVALID)."""
        ...

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
        """Finalize an upload into the channel; returns (message_id, doc_id)."""
        ...

    async def copy_message(
        self,
        from_channel_id: int,
        message_id: int,
        to_channel_id: int,
        *,
        caption: str | None = None,
    ) -> tuple[int, int]:
        """Re-send a message to another channel (no forward header);
        when `caption` is provided it replaces the source message caption;
        returns the new (message_id, doc_id)."""
        ...

    async def delete_message(self, channel_id: int, message_id: int) -> bool:
        """Delete a message; False if it was already gone."""
        ...

    async def edit_message_caption(
        self, channel_id: int, message_id: int, caption: str
    ) -> None:
        """Replace the text caption of an existing file message."""
        ...
