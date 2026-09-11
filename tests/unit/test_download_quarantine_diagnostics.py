from __future__ import annotations

from tgshelf.core.download import ParallelStreamer
from tgshelf.telegram.pool import BotPool, PoolMember


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_unavailable_cause_includes_next_local_probe() -> None:
    clock = Clock()
    member = PoolMember(object(), "c_f1_bot")
    pool = BotPool([member], quarantine_backoff=(300.0,), clock=clock)
    streamer = ParallelStreamer(pool, k=1)
    for _ in range(3):
        pool.mark_error(member)

    assert streamer._unavailable_bot_cause(-100) == (
        "1 bot quarantined; next probe in 300s"
    )


def test_unavailable_cause_reports_probe_in_progress() -> None:
    clock = Clock()
    member = PoolMember(object(), "h_f1_bot")
    pool = BotPool([member], quarantine_backoff=(300.0,), clock=clock)
    streamer = ParallelStreamer(pool, k=1)
    for _ in range(3):
        pool.mark_error(member)
    clock.value = 300.0
    assert pool.lease(1, allow_probe=True) == [member]

    assert streamer._unavailable_bot_cause(-100) == (
        "1 bot quarantined; half-open probe in progress"
    )
