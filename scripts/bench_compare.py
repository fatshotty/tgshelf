"""Compare two download-bench logs (legacy vs current) side by side.

Reads the per-chunk `[fetch] ... in <dt>s` lines, the `[flood]`/`[failover]`/
`[quarantine]`/`[wait]` markers and the final `RECAP ...` line from each log
file and prints a side-by-side table: avg MB/s, the chunk-fetch dt distribution
(mean/p50/p95/max), gaps/stalls, flood and failover counts, quarantine.

Usage:
  python scripts/bench_compare.py /tmp/bench_legacy.log /tmp/bench_current.log
"""

from __future__ import annotations

import re
import sys

_DT = re.compile(r"\[fetch\].*? in (\d+\.\d+)s")
_RECAP = re.compile(r"RECAP (.+)")


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, int(q * (len(s) - 1) + 0.5))
    return s[i]


def parse(path: str) -> dict:
    dts: list[float] = []
    floods = failovers = quarantines = waits = timeouts = 0
    recap: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _DT.search(line)
            if m:
                dts.append(float(m.group(1)))
            if "[flood]" in line:
                floods += 1
            if "[failover]" in line:
                failovers += 1
            if "[quarantine]" in line:
                quarantines += 1
            if "[wait]" in line:
                waits += 1
            if "timed out" in line:
                timeouts += 1
            r = _RECAP.search(line)
            if r:
                for tok in r.group(1).split():
                    if "=" in tok:
                        k, v = tok.split("=", 1)
                        recap[k] = v
    return {
        "path": path,
        "dts": dts,
        "floods": floods,
        "failovers": failovers,
        "quarantines": quarantines,
        "waits": waits,
        "timeouts": timeouts,
        "recap": recap,
    }


def _row(label: str, a: str, b: str) -> str:
    return f"{label:<22}{a:>20}{b:>20}"


def report(a: dict, b: dict) -> None:
    ra, rb = a["recap"], b["recap"]

    def g(r, k, default="-"):
        return r.get(k, default)

    print(_row("metric", ra.get("path", "A"), rb.get("path", "B")))
    print("-" * 62)
    print(_row("avg MB/s", g(ra, "avg"), g(rb, "avg")))
    print(_row("total time (s)", g(ra, "time"), g(rb, "time")))
    print(_row("bytes", g(ra, "bytes"), g(rb, "bytes")))
    print(_row("chunks", g(ra, "chunks"), g(rb, "chunks")))
    print("-" * 62)
    for name, key in (("dt mean", "mean"), ("dt p50", "p50"), ("dt p95", "p95"), ("dt max", "max")):
        va = _fmt_stat(a["dts"], key)
        vb = _fmt_stat(b["dts"], key)
        print(_row(name, va, vb))
    print("-" * 62)
    print(_row("floods", str(a["floods"]), str(b["floods"])))
    print(_row("flood_time (s)", g(ra, "flood_time"), g(rb, "flood_time")))
    print(_row("failovers", str(a["failovers"]), str(b["failovers"])))
    print(_row("waits ([wait])", str(a["waits"]), str(b["waits"])))
    print(_row("timeouts", str(a["timeouts"]), str(b["timeouts"])))
    print(_row("quarantine events", str(a["quarantines"]), str(b["quarantines"])))
    print(_row("quarantined (recap)", g(ra, "quarantined"), g(rb, "quarantined")))


def _fmt_stat(dts: list[float], key: str) -> str:
    if not dts:
        return "-"
    if key == "mean":
        return f"{sum(dts) / len(dts):.2f}"
    if key == "max":
        return f"{max(dts):.2f}"
    if key == "p50":
        return f"{_pct(dts, 0.50):.2f}"
    if key == "p95":
        return f"{_pct(dts, 0.95):.2f}"
    return "-"


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: bench_compare.py <log_a> <log_b>  (e.g. legacy current)")
    a = parse(sys.argv[1])
    b = parse(sys.argv[2])
    report(a, b)


if __name__ == "__main__":
    main()
