from __future__ import annotations

from tgshelf.http.ops import _pool_status
from tgshelf.telegram.pool import BotPool, PoolMember


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_pool_status_exposes_quarantine_countdown_and_attempt() -> None:
    clock = Clock()
    member = PoolMember(object(), "bot")
    pool = BotPool([member], quarantine_backoff=(300.0,), clock=clock)
    for _ in range(3):
        pool.mark_error(member)

    status = _pool_status(pool)

    assert status["next_quarantine_probe_in"] == 300.0
    assert status["members"][0]["state"] == "quarantined"
    assert status["members"][0]["half_open"] is False
    assert status["members"][0]["quarantine_attempt"] == 1
    assert status["members"][0]["quarantine_remaining"] == 300.0
    assert status["members"][0]["cooldown_remaining"] == 0.0


def test_pool_status_exposes_half_open_probe() -> None:
    clock = Clock()
    member = PoolMember(object(), "bot")
    pool = BotPool([member], quarantine_backoff=(300.0,), clock=clock)
    for _ in range(3):
        pool.mark_error(member)
    clock.value = 300.0
    assert pool.lease(1, allow_probe=True) == [member]

    status = _pool_status(pool)

    assert status["next_quarantine_probe_in"] is None
    assert status["members"][0]["state"] == "half_open"
    assert status["members"][0]["half_open"] is True
    assert status["members"][0]["quarantine_remaining"] == 0.0


def test_pool_status_returns_to_healthy_after_probe_success() -> None:
    clock = Clock()
    member = PoolMember(object(), "bot")
    pool = BotPool([member], quarantine_backoff=(300.0,), clock=clock)
    for _ in range(3):
        pool.mark_error(member)
    clock.value = 300.0
    assert pool.lease(1, allow_probe=True) == [member]
    pool.mark_success(member)

    status = _pool_status(pool)

    assert status["available"] == 1
    assert status["next_quarantine_probe_in"] is None
    assert status["members"][0]["state"] == "healthy"
    assert status["members"][0]["quarantined"] is False
