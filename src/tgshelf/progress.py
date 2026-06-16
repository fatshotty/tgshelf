"""Generic, dependency-free download progress: counters, sliding-window speed,
and the strings for the live block / recap / error log. No DB, no Telegram, no
ANSI here (painting lives in the command) — everything is pure and unit-tested.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque

_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in _UNITS:
        if n < 1024 or unit == _UNITS[-1]:
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_rate(bytes_per_sec: float) -> str:
    return f"{human_bytes(bytes_per_sec)}/s"


def human_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m}m{sec}s"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


@dataclass
class FileSnap:
    name: str
    size: int
    done: int
    rate: float


@dataclass
class Snapshot:
    total_files: int
    total_bytes: int
    ok: int
    skipped: int
    failed: int
    remaining: int
    bytes_done: int
    overall_rate: float
    files: list[FileSnap]
    elapsed: float


@dataclass
class _Active:
    name: str
    size: int
    done: int = 0
    samples: Deque[tuple[float, int]] = field(default_factory=deque)


class ProgressState:
    """Shared, single-loop progress state. `snapshot()` is the single timing point
    (records a sample, prunes the window, computes rates), so speed is testable
    with an injected clock."""

    def __init__(self, total_files: int, total_bytes: int, *,
                 clock: Callable[[], float] = time.monotonic, window: float = 1.0):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.ok = 0
        self.skipped = 0
        self.failed = 0
        self.bytes_done = 0
        self.active: dict[str, _Active] = {}
        self._clock = clock
        self._window = window
        self._start: float | None = None  # set lazily on the first snapshot
        self._samples: Deque[tuple[float, int]] = deque()

    def start_file(self, key: str, name: str, size: int) -> None:
        self.active[key] = _Active(name, size)

    def advance(self, key: str, nbytes: int) -> None:
        fp = self.active.get(key)
        if fp is not None:
            fp.done += nbytes
        self.bytes_done += nbytes

    def finish(self, key: str, status: str) -> None:
        fp = self.active.pop(key, None)
        if status == "ok":
            self.ok += 1
        elif status == "skipped":
            self.skipped += 1
            if fp is not None:
                self.bytes_done += max(0, fp.size - fp.done)
        else:
            self.failed += 1

    @property
    def remaining(self) -> int:
        return self.total_files - (self.ok + self.skipped + self.failed)

    @staticmethod
    def _rate(samples: Deque[tuple[float, int]]) -> float:
        if len(samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = samples[0], samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def _prune(self, samples: Deque[tuple[float, int]], now: float) -> None:
        while len(samples) > 1 and now - samples[0][0] > self._window:
            samples.popleft()

    def snapshot(self) -> Snapshot:
        now = self._clock()
        if self._start is None:
            self._start = now
        self._samples.append((now, self.bytes_done))
        self._prune(self._samples, now)
        files = []
        for fp in self.active.values():
            fp.samples.append((now, fp.done))
            self._prune(fp.samples, now)
            files.append(FileSnap(fp.name, fp.size, fp.done, self._rate(fp.samples)))
        return Snapshot(
            self.total_files, self.total_bytes, self.ok, self.skipped, self.failed,
            self.remaining, self.bytes_done, self._rate(self._samples), files,
            now - self._start,
        )
