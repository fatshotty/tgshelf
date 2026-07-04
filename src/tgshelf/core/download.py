"""Streaming download: range math (StreamPlan) and the multi-bot streamer.

A file is its parts concatenated into one byte space. An HTTP range
[start, end] (inclusive) maps to a slice of that space; StreamPlan cuts it into
1MiB-aligned GetFile requests (offset always a multiple of the chunk size, so
Telegram's alignment rule is satisfied for free) plus the trim to apply to each
fetched window before handing bytes to the client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

from tgshelf.constants import CHUNK_SIZE

log = logging.getLogger("tgshelf.download")
from tgshelf.telegram.errors import (
    ChannelUnavailable,
    FileRefExpired,
    FloodCooldown,
    PartMissing,
)
from tgshelf.telegram.pool import BotPool, ClientPool, PoolMember


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


@dataclass
class _StreamState:
    cond: asyncio.Condition
    n: int
    capacity: int = 1  # max chunks dispatched-but-not-emitted (the read-ahead
                       # buffer). Decoupled from the worker count so the K bots
                       # keep fetching ahead instead of idling until the emitter
                       # catches up — that idle was the periodic flush gap.
    next_dispatch: int = 0
    next_emit: int = 0
    results: dict = field(default_factory=dict)  # seq -> trimmed bytes
    error: BaseException | None = None


class ParallelStreamer:
    """Streams a file's range over K bots with a read-ahead buffer and transparent
    failover. K worker tasks pull the next chunk as soon as they are free — bounded
    only by the buffer capacity C = 2*K (K in flight + K fetched ahead), NOT by the
    emit position — so the bots never idle waiting for the in-order chunk. The
    emitter yields strictly in seq order; RAM per stream ≈ C chunks. A worker whose
    bot floods/times-out/loses the channel swaps to a replacement and requeues the
    chunk; FileRefExpired re-resolves the part once (per-bot). The client sees only
    ordered bytes, never the reshuffling underneath.
    """

    def __init__(
        self,
        bot_pool: BotPool,
        *,
        k: int,
        chunk_size: int = CHUNK_SIZE,
        chunk_timeout: float = 6.0,
        max_retries: int = 3,
        user_pool: ClientPool | None = None,
        allow_user_fallback: bool = False,
        memory_soft_limit: int = 0,
        sleep=asyncio.sleep,
    ):
        self._bot_pool = bot_pool
        self._k = k
        self._chunk_size = chunk_size
        self._chunk_timeout = chunk_timeout
        self._max_retries = max_retries
        self._user_pool = user_pool
        self._allow_user_fallback = allow_user_fallback
        self._memory_soft_limit = memory_soft_limit
        self._sleep = sleep
        # observability (surfaced at /metrics)
        self._active_streams = 0
        self._buffered_bytes = 0  # estimated: sum of K×chunk over active streams
        self._streams_total = 0
        self._bytes_total = 0
        self._degraded_total = 0

    def metrics(self) -> dict:
        return {
            "configured_k": self._k,
            "memory_soft_limit": self._memory_soft_limit,
            "active_streams": self._active_streams,
            "buffered_bytes": self._buffered_bytes,
            "streams_total": self._streams_total,
            "bytes_total": self._bytes_total,
            "degraded_total": self._degraded_total,
        }

    async def stream(
        self, parts: Sequence[Any], plan: StreamPlan, channel_id: int
    ) -> AsyncIterator[bytes]:
        chunks = plan.chunks

        # K = bots fetching in parallel for this stream (config multi_bot_download).
        # The read-ahead buffer is C = 2*K: K chunks in flight + K already fetched
        # and waiting, so a bot that finishes a chunk immediately grabs the next
        # instead of idling until the in-order chunk is emitted. RAM per stream is
        # bounded by C chunks. Optional soft-limit degradation: when the estimated
        # buffers would blow past memory_soft_limit, a NEW stream starts at K=1 —
        # degraded, never refused. Decision + reservation are synchronous (no await
        # between), so concurrent starts don't race.
        k = self._k
        capacity = 2 * k
        if (
            self._memory_soft_limit > 0
            and self._buffered_bytes + capacity * self._chunk_size > self._memory_soft_limit
        ):
            k = 1
            capacity = 2
            self._degraded_total += 1
            log.warning(
                "[degraded] buffered %d B; a new stream would exceed soft "
                "limit %d B -> starting at K=1",
                self._buffered_bytes, self._memory_soft_limit,
            )
        reserve = capacity * self._chunk_size
        self._buffered_bytes += reserve
        self._active_streams += 1
        self._streams_total += 1
        try:
            state = _StreamState(cond=asyncio.Condition(), n=len(chunks), capacity=capacity)
            part_refs: dict[tuple[str, int], Any] = {}  # (bot name, part idx) -> DocRef
            # bots that turned out unable to reach this channel: excluded for the
            # REST OF THIS STREAM ONLY (not persisted in the pool). A bot you
            # re-add to the channel is re-checked on the NEXT stream, no restart.
            unreachable: list = []

            # generic lease (works whether the pool is a BotPool or, when no bots
            # are configured, the user ClientPool)
            bots = self._bot_pool.lease(k, channel_id=channel_id)
            if not bots:
                bots = [await self._replace(channel_id, exclude=[])]
            log.debug(
                "[stream] channel %s, %d chunk(s), K=%d, read-ahead=%d, clients %s",
                channel_id, len(chunks), k, capacity, [b.name for b in bots],
            )

            workers = [
                asyncio.create_task(
                    self._worker(state, chunks, parts, channel_id, part_refs, bot, unreachable))
                for bot in bots
            ]
            try:
                for seq in range(state.n):
                    async with state.cond:
                        while seq not in state.results and state.error is None:
                            await state.cond.wait()
                        if state.error is not None:
                            raise state.error
                        data = state.results.pop(seq)
                        state.next_emit = seq + 1
                        # occupancy = dispatched-but-not-emitted (in flight + ready).
                        # ≈ capacity -> buffer full = client-paced backpressure (idle
                        # bots are normal); ≈ 0 -> starving = bots can't keep up.
                        occupancy = state.next_dispatch - state.next_emit
                        state.cond.notify_all()
                    if seq % 16 == 0 or occupancy == 0:
                        log.debug("[buf] seq %d buffer %d/%d (ready %d)%s",
                                  seq, occupancy, state.capacity, len(state.results),
                                  "  STARVED" if occupancy == 0 else "")
                    self._bytes_total += len(data)
                    yield data
            finally:
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
        finally:
            self._buffered_bytes -= reserve
            self._active_streams -= 1

    async def _worker(self, state, chunks, parts, channel_id, part_refs, bot, unreachable):
        while True:
            async with state.cond:
                while True:
                    if state.error is not None or state.next_dispatch >= state.n:
                        return
                    # dispatch as long as the read-ahead buffer has room, gated by
                    # capacity (= 2*K) and NOT by next_emit: a free bot grabs the
                    # next chunk immediately instead of waiting for the in-order one
                    # to be emitted, so the K bots never idle between batches.
                    if state.next_dispatch < state.next_emit + state.capacity:
                        idx = state.next_dispatch
                        state.next_dispatch += 1
                        break
                    await state.cond.wait()
            chunk = chunks[idx]
            try:
                data, bot = await self._fetch(chunk, parts, channel_id, part_refs, bot, unreachable)
            except BaseException as exc:  # noqa: BLE001 - propagated to the emitter
                async with state.cond:
                    if state.error is None:
                        state.error = exc
                    state.cond.notify_all()
                return
            async with state.cond:
                state.results[chunk.seq] = data
                state.cond.notify_all()

    async def _fetch(self, chunk, parts, channel_id, part_refs, bot, unreachable):
        p = parts[chunk.part_idx]
        retries = 0
        last_exc: BaseException | None = None
        while True:
            # file_reference (and the document access_hash) are bound to the
            # ACCOUNT that resolved them, so the cache is per-client: keying by
            # (bot, part) stops one bot from reusing another bot's reference,
            # which Telegram rejects as FILE_REFERENCE_EXPIRED (the churn that
            # collapsed throughput on seek). `bot` may change on replacement, so
            # the key is recomputed each iteration.
            cache_key = (bot.name, chunk.part_idx)
            self._bot_pool.acquire(bot)
            need_replacement = False
            try:
                # RESOLVE is inside the failover loop too (timeout + replace): a bot
                # that can't reach the channel raises ChannelUnavailable here ->
                # logged [eligibility]/[failover] + swapped; a hung get_document is
                # bounded by chunk_timeout instead of stalling the stream forever.
                ref = part_refs.get(cache_key)
                if ref is None:
                    log.debug("[fetch] resolving part %d (msg %s @ channel %s) via '%s'",
                              chunk.part_idx, p.message_id, p.channel_id, bot.name)
                    t_r = time.monotonic()
                    ref = await asyncio.wait_for(
                        bot.client.get_document(p.channel_id, p.message_id),
                        timeout=self._chunk_timeout,
                    )
                    if ref is None:
                        raise PartMissing(file_path=str(p.message_id), part_idx=chunk.part_idx)
                    part_refs[cache_key] = ref
                    log.debug("[fetch] resolved part %d (dc %s) via '%s' in %.2fs",
                              chunk.part_idx, getattr(ref, "dc_id", None), bot.name,
                              time.monotonic() - t_r)
                t0 = time.monotonic()
                raw = await asyncio.wait_for(
                    bot.client.get_file_chunk(ref, chunk.offset, chunk.limit),
                    timeout=self._chunk_timeout,
                )
                dt = time.monotonic() - t0
                self._bot_pool.mark_success(bot)
                log.debug(
                    "[fetch] chunk %d part %d (msg %s, dc %s) <- '%s' (%d B) in %.2fs",
                    chunk.seq, chunk.part_idx, p.message_id,
                    getattr(ref, "dc_id", None), bot.name, len(raw), dt,
                )
                return raw[chunk.trim_start : chunk.trim_end], bot
            except FloodCooldown as exc:
                self._bot_pool.mark_flood(bot, exc.seconds)
                log.info("[failover] chunk %d: '%s' flooded; replacing bot", chunk.seq, bot.name)
                last_exc, need_replacement = exc, True
            except FileRefExpired as exc:
                log.debug("chunk %d: file_reference expired; re-resolving part %d via '%s'",
                          chunk.seq, chunk.part_idx, bot.name)
                part_refs.pop(cache_key, None)  # re-resolve for THIS bot only
                last_exc = exc
            except ChannelUnavailable as exc:
                # per-stream exclusion (NOT persisted): a re-added bot is rechecked
                # on the next stream. Other workers see it via the shared list.
                if bot not in unreachable:
                    unreachable.append(bot)
                log.warning("[eligibility] chunk %d: '%s' cannot reach channel %s; "
                            "excluded for this stream", chunk.seq, bot.name, channel_id)
                last_exc, need_replacement = exc, True
            except asyncio.TimeoutError as exc:
                self._bot_pool.mark_error(bot)
                log.warning("[failover] chunk %d: '%s' timed out (>%.0fs at resolve/fetch); replacing",
                            chunk.seq, bot.name, self._chunk_timeout)
                last_exc, need_replacement = exc, True
            finally:
                self._bot_pool.release(bot)

            retries += 1
            if retries > self._max_retries:
                raise last_exc
            if need_replacement:
                # exclude the per-stream unreachable set + the bot that just failed
                # (flood/timeout recover via the pool, so they're excluded only for
                # this immediate pick).
                bot = await self._replace(channel_id, exclude=unreachable + [bot])

    async def _replace(self, channel_id: int, exclude: Sequence[PoolMember]) -> PoolMember:
        repl = self._bot_pool.replace(channel_id=channel_id, exclude=exclude)
        if repl is not None:
            return repl

        # bot pool exhausted: optional user fallback before waiting
        if self._allow_user_fallback and self._user_pool is not None:
            user = self._user_pool.lease_one()
            if user is not None:
                return user

        # otherwise wait for a bot to free up (shared lease+wait logic)
        repl = await self._bot_pool.lease_or_wait(
            channel_id=channel_id, exclude=exclude, sleep=self._sleep
        )
        if repl is None:
            raise ChannelUnavailable(f"no client can reach channel {channel_id}")
        return repl
