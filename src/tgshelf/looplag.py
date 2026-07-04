"""Event-loop lag monitor (diagnostic).

A coroutine that sleeps a short interval in a loop and measures how late it is
actually resumed. If the wake-up is delayed well past the interval, the event
loop was blocked by synchronous work (CPU-bound decryption, a blocking I/O call,
a long GC pause, …) — which freezes *every* in-flight await at once. That is the
signature to look for when "all bots stall together" periodically.

Marker: `[looplag]`. Logged at WARNING so it shows regardless of level once the
monitor is running; callers start it only when diagnosing (debug).
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("tgshelf.looplag")


def start_loop_lag_monitor(*, interval: float = 0.1, threshold: float = 0.25) -> asyncio.Task:
    """Start the monitor and return its task. Stop it with
    `await stop_loop_lag_monitor(task)`. `interval` is how often it probes the
    loop; `threshold` is the extra delay (seconds) above `interval` that counts
    as a stall worth logging."""

    async def run() -> None:
        try:
            while True:
                t0 = time.monotonic()
                await asyncio.sleep(interval)
                lag = time.monotonic() - t0 - interval
                if lag > threshold:
                    log.warning("[looplag] event loop blocked for %.2fs", lag)
        except asyncio.CancelledError:
            pass

    return asyncio.create_task(run())


async def stop_loop_lag_monitor(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
