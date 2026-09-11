from __future__ import annotations

import pytest

from tgshelf.telegram.pool import BotPool, PoolMember


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_pool(*, members: int = 1) -> tuple[BotPool, list[PoolMember], Clock]:
    clock = Clock()
    pool_members = [
        PoolMember(object(), name=f"bot-{index}") for index in range(1, members + 1)
    ]
    pool = BotPool(
        pool_members,
        max_errors=3,
        quarantine_backoff=(300.0, 900.0, 1800.0, 3600.0),
        clock=clock,
    )
    return pool, pool_members, clock


def quarantine(pool: BotPool, member: PoolMember) -> None:
    pool.mark_error(member)
    pool.mark_error(member)
    pool.mark_error(member)


def test_third_consecutive_error_starts_first_quarantine_period() -> None:
    pool, members, _clock = make_pool()
    member = members[0]

    quarantine(pool, member)

    assert member.quarantined is True
    assert member.half_open is False
    assert member.quarantine_attempt == 1
    assert member.quarantine_until == 300.0
    assert pool.lease(1, channel_id=-100, allow_probe=True) == []


def test_expired_quarantine_leases_exactly_one_half_open_probe() -> None:
    pool, members, clock = make_pool(members=2)
    first, second = members
    quarantine(pool, first)
    quarantine(pool, second)
    clock.advance(300.0)

    assert pool.lease(1, channel_id=-100, allow_probe=True) == [first]
    assert first.half_open is True
    assert pool.lease(1, channel_id=-100, allow_probe=True) == []


def test_half_open_success_fully_recovers_member() -> None:
    pool, members, clock = make_pool()
    member = members[0]
    quarantine(pool, member)
    clock.advance(300.0)
    assert pool.lease(1, channel_id=-100, allow_probe=True) == [member]

    pool.mark_success(member)

    assert member.quarantined is False
    assert member.half_open is False
    assert member.quarantine_attempt == 0
    assert member.quarantine_until == 0.0
    assert member.consecutive_errors == 0
    assert pool.lease(1, channel_id=-100, allow_probe=True) == [member]


@pytest.mark.parametrize(
    ("attempt", "expected_delay"),
    [(2, 900.0), (3, 1800.0), (4, 3600.0), (5, 3600.0)],
)
def test_failed_half_open_probe_advances_backoff_until_cap(
    attempt: int, expected_delay: float
) -> None:
    pool, members, clock = make_pool()
    member = members[0]
    quarantine(pool, member)

    for expected_attempt in range(2, attempt + 1):
        clock.value = member.quarantine_until
        assert pool.lease(1, channel_id=-100, allow_probe=True) == [member]
        before = clock.value
        pool.mark_error(member)
        assert member.quarantine_attempt == expected_attempt

    assert member.half_open is False
    assert member.quarantine_until - before == expected_delay


def test_in_flight_errors_do_not_advance_an_existing_quarantine() -> None:
    pool, members, _clock = make_pool()
    member = members[0]
    quarantine(pool, member)
    deadline = member.quarantine_until

    pool.mark_error(member)

    assert member.quarantine_attempt == 1
    assert member.quarantine_until == deadline


def test_due_probe_is_included_while_healthy_members_keep_serving() -> None:
    pool, members, clock = make_pool(members=3)
    recovering, healthy_one, healthy_two = members
    quarantine(pool, recovering)
    clock.advance(300.0)

    leased = pool.lease(2, channel_id=-100, allow_probe=True)

    assert leased == [recovering, healthy_one]
    assert healthy_two.half_open is False


def test_future_quarantine_reports_wait_without_sleeping_request() -> None:
    pool, members, _clock = make_pool()
    quarantine(pool, members[0])

    assert pool.next_quarantine_wait(channel_id=-100) == 300.0


def test_flood_cooldown_remains_independent_from_quarantine() -> None:
    pool, members, clock = make_pool()
    member = members[0]

    pool.mark_flood(member, 30.0)
    clock.advance(30.0)

    assert member.quarantined is False
    assert member.quarantine_attempt == 0
    assert pool.lease(1, channel_id=-100, allow_probe=True) == [member]
