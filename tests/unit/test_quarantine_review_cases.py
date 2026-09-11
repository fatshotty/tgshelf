from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgshelf.core.download import ParallelStreamer
from tgshelf.telegram.errors import ChannelUnavailable, FloodCooldown
from tgshelf.telegram.pool import BotPool, ClientPool, PoolMember


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def quarantine(pool, member: PoolMember) -> None:
    for _ in range(3):
        pool.mark_error(member)


def test_client_pool_normal_leases_do_not_consume_due_probe() -> None:
    clock = Clock()
    member = PoolMember(object(), "user")
    pool = ClientPool([member], quarantine_backoff=(10.0,), clock=clock)
    quarantine(pool, member)
    clock.value = 10.0

    assert pool.lease_one() is None
    assert member.half_open is False
    assert pool.lease(1, allow_probe=True) == [member]


@pytest.mark.asyncio
async def test_lease_or_wait_does_not_hide_earlier_quarantine_behind_cooldown() -> None:
    clock = Clock()
    probe = PoolMember(object(), "probe")
    cooling = PoolMember(object(), "cooling")
    pool = BotPool([probe, cooling], quarantine_backoff=(10.0,), clock=clock)
    quarantine(pool, probe)
    pool.mark_flood(cooling, 100.0)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    member = await pool.lease_or_wait(allow_probe=True, sleep=sleep)

    assert member is None
    assert sleeps == []
    assert pool.next_quarantine_wait() == 10.0


@pytest.mark.asyncio
async def test_pool_exhaustion_error_does_not_claim_missing_membership() -> None:
    clock = Clock()
    member = PoolMember(object(), "bot")
    pool = BotPool([member], quarantine_backoff=(300.0,), clock=clock)
    quarantine(pool, member)
    streamer = ParallelStreamer(pool, k=1)

    with pytest.raises(ChannelUnavailable) as raised:
        await streamer._replace(-100, exclude=[])

    message = str(raised.value)
    assert "next probe in 300s" in message
    assert "client is not a member" not in message
    assert "bots check" not in message


class Notifier:
    def __init__(self) -> None:
        self.payload = None

    async def notify(self, payload, **_kwargs) -> None:
        self.payload = payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("quarantine", "automatic probe"),
        ("cooldown", "cooldown"),
        ("ineligible", "bots check"),
    ],
)
async def test_pool_exhaustion_action_matches_cause(state: str, expected: str) -> None:
    clock = Clock()
    member = PoolMember(object(), "bot")
    pool = BotPool([member], quarantine_backoff=(300.0,), clock=clock)
    if state == "quarantine":
        quarantine(pool, member)
    elif state == "cooldown":
        pool.mark_flood(member, 30.0)
    else:
        member.ineligible_channels.add(-100)
    notifier = Notifier()
    streamer = ParallelStreamer(pool, k=1, notifier=notifier)

    await streamer._notify_pool_exhausted(-100, fallback=False)

    assert expected in notifier.payload["action"]


class FloodedClient:
    async def get_document(self, _channel_id: int, _message_id: int):
        raise FloodCooldown(45)


@pytest.mark.asyncio
async def test_flooded_half_open_probe_defers_to_flood_cooldown() -> None:
    clock = Clock()
    member = PoolMember(FloodedClient(), "bot")
    pool = BotPool([member], quarantine_backoff=(300.0,), clock=clock)
    quarantine(pool, member)
    clock.value = 300.0
    assert pool.lease(1, allow_probe=True) == [member]
    streamer = ParallelStreamer(pool, k=1, max_retries=0)
    chunk = SimpleNamespace(
        seq=0,
        part_idx=0,
        offset=0,
        limit=1,
        trim_start=0,
        trim_end=1,
    )
    part = SimpleNamespace(channel_id=-100, message_id=1)

    with pytest.raises(FloodCooldown):
        await streamer._fetch(chunk, [part], -100, {}, member, [])

    assert member.quarantined is True
    assert member.half_open is False
    assert pool.next_quarantine_wait(-100) == 45.0
