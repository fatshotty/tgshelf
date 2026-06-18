"""Live terminal rendering for long CLI tasks (download / sync).

The progress math + strings live in `progress.py` (pure, ANSI-free, unit-tested);
this module owns the ANSI painting and the async repaint loop, shared by the
commands so each gets the same TTY block + non-TTY fallback.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time

from tgshelf.progress import ProgressState, build_block


async def run_render_loop(
    state: ProgressState,
    *,
    header: str,
    stop: asyncio.Event,
    log: logging.Logger,
    marker: str,
    interval: float = 0.2,
) -> None:
    """Repaint the live block in place (TTY) until `stop` is set. Non-TTY: a
    periodic plain status line via `log`, tagged `[<marker>]`. Never raises —
    rendering must never crash the command."""
    tty = sys.stdout.isatty()
    prev_lines = 0
    last_plain = 0.0
    try:
        while not stop.is_set():
            snap = state.snapshot()
            if tty:
                # Truncate every line to the terminal width: a wrapped line would
                # occupy >1 physical row, desyncing the cursor-up count below and
                # making the block "stair-step" / duplicate the header.
                width = max(1, shutil.get_terminal_size((80, 24)).columns)
                lines = [ln[:width] for ln in build_block(snap, header=header)]
                out = ""
                if prev_lines:
                    out += f"\033[{prev_lines}F"  # cursor up to block start
                out += "\033[J"                   # clear from cursor to end of screen
                for ln in lines:
                    out += ln + "\n"
                sys.stdout.write(out)
                sys.stdout.flush()
                prev_lines = len(lines)
            else:
                now = time.monotonic()
                if now - last_plain >= 3.0:
                    last_plain = now
                    log.info("[%s] %s | ok %d skip %d err %d remaining %d", marker,
                             build_block(snap, header="")[2], snap.ok, snap.skipped,
                             snap.failed, snap.remaining)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - rendering must never crash the command
        log.debug("[%s] renderer error", marker, exc_info=True)
