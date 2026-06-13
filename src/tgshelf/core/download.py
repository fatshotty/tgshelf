"""Streaming download: range math (StreamPlan) and the multi-bot streamer.

A file is its parts concatenated into one byte space. An HTTP range
[start, end] (inclusive) maps to a slice of that space; StreamPlan cuts it into
1MiB-aligned GetFile requests (offset always a multiple of the chunk size, so
Telegram's alignment rule is satisfied for free) plus the trim to apply to each
fetched window before handing bytes to the client.
"""

from __future__ import annotations

from dataclasses import dataclass

from tgshelf.constants import CHUNK_SIZE


class RangeNotSatisfiable(Exception):
    """Requested range is outside [0, total_size) — HTTP 416."""


@dataclass(frozen=True)
class ChunkReq:
    seq: int          # global emit order
    part_idx: int     # which part of the file this chunk reads from
    offset: int       # GetFile offset within the part (multiple of chunk size)
    limit: int        # GetFile limit (the chunk size)
    trim_start: int   # first byte to keep within the fetched window
    trim_end: int     # one past the last byte to keep (exclusive)


@dataclass(frozen=True)
class StreamPlan:
    chunks: tuple[ChunkReq, ...]
    range_start: int
    range_end: int
    total_size: int

    @property
    def content_length(self) -> int:
        return self.range_end - self.range_start + 1

    @classmethod
    def build(
        cls,
        part_sizes,
        start: int,
        end: int,
        *,
        chunk_size: int = CHUNK_SIZE,
    ) -> "StreamPlan":
        total = sum(part_sizes)
        if total == 0 or start < 0 or start > end or end >= total:
            raise RangeNotSatisfiable(
                f"range {start}-{end} not satisfiable for size {total}"
            )

        chunks: list[ChunkReq] = []
        seq = 0
        part_start = 0
        for part_idx, size in enumerate(part_sizes):
            part_end = part_start + size - 1  # inclusive global offset
            if size > 0 and start <= part_end and end >= part_start:
                local_lo = max(start, part_start) - part_start
                local_hi = min(end, part_end) - part_start  # inclusive
                aligned = local_lo - (local_lo % chunk_size)
                off = aligned
                while off <= local_hi:
                    window_end = min(off + chunk_size, size)  # bytes available
                    trim_start = max(local_lo, off) - off
                    trim_end = min(local_hi, window_end - 1) - off + 1
                    chunks.append(
                        ChunkReq(
                            seq=seq,
                            part_idx=part_idx,
                            offset=off,
                            limit=chunk_size,
                            trim_start=trim_start,
                            trim_end=trim_end,
                        )
                    )
                    seq += 1
                    off += chunk_size
            part_start += size

        return cls(
            chunks=tuple(chunks),
            range_start=start,
            range_end=end,
            total_size=total,
        )
