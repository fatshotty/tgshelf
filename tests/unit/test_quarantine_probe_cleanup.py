from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tgshelf.core.download import ParallelStreamer, _StreamState
from tgshelf.telegram.pool import BotPool, PoolMember


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class BlockingClient:
    async def get_document(self, channel_id: int, message_id: int):
        await asyncio.Event().wait()


def quarantined_probe() -> tuple[BotPool, PoolMember, Clock]:
    clock = Clock()
    member = PoolMember(BlockingClient(), "bot")
    pool = BotPool([member], quarantine_backoff=(300.0,), clock=clock)
    for _ in range(3):
        pool.mark_error(member)
    clock.value = 300.0
    assert pool.lease(1, channel_id=-100, allow_probe=True) == [member]
    return pool, member, clock


def test_aborted_probe_remains_quarantined_and_becomes_due_again() -> None:
    pool, member, _clock = quarantined_probe()

    pool.abort_probe(member)

    assert member.quarantined is True
    assert member.half_open is False
    assert member.quarantine_attempt == 1
    assert pool.next_quarantine_wait(channel_id=-100) == 0.0


@pytest.mark.asyncio
async def test_cancelled_fetch_releases_half_open_probe() -> None:
    pool, member, _clock = quarantined_probe()
    streamer = ParallelStreamer(pool, k=1)
    chunk = SimpleNamespace(
        seq=0,
        part_idx=0,
        offset=0,
        limit=1,
        trim_start=0,
        trim_end=1,
    )
    part = SimpleNamespace(channel_id=-100, message_id=1)
    task = asyncio.create_task(
        streamer._fetch(chunk, [part], -100, {}, member, [])
    )
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert member.half_open is False
    assert member.quarantined is True
    assert member.in_flight == 0


@pytest.mark.asyncio
async def test_worker_without_a_chunk_releases_half_open_probe() -> None:
    pool, member, _clock = quarantined_probe()
    streamer = ParallelStreamer(pool, k=1)
    state = _StreamState(cond=asyncio.Condition(), n=0)

    await streamer._worker(state, [], [], -100, {}, member, [])

    assert member.half_open is False
    assert member.quarantined is True
    assert pool.next_quarantine_wait(channel_id=-100) == 0.0
