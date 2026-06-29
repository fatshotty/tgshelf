# Sync Progress And Log File Design

## Goal

Give `tgshelf sync` the same operator feedback quality as `download`: live
progress, final recap, and an optional full-run log file.

## Progress

The sync command uses the shared renderer from `src/tgshelf/render.py`.

- TTY output is live and multi-line.
- non-TTY output is stable line-oriented text.
- Per-file progress comes from a counting source wrapper around file reads.
- Final stats remain authoritative from the sync `Stats` object.

## Log File

`--log-file PATH` adds a file handler for the run. It captures `[sync]` events
without changing normal stdout/stderr progress behavior.

## Verification

Tests cover renderer output, progress counters, recap text, log-file wiring, and
sync stats for uploaded, skipped, mismatched, overwritten, and deleted files.
