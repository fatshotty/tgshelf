# Sync Progress And Log File Implementation Plan

## Goal

Make `sync` operator output match the quality of `download`.

## Completed Tasks

- Extract shared rendering primitives into `src/tgshelf/render.py`.
- Wrap file sources with a counting reader for per-file byte progress.
- Add live TTY sync progress.
- Add stable non-TTY progress.
- Add final recap that includes uploaded, skipped, mismatched, overwritten,
  deleted, warning, and error counts.
- Add `--log-file PATH` and file-handler wiring for `[sync]` events.
- Add tests for renderer output, recap formatting, and log-file behavior.

## Important Constraints

- `Stats` remains the source of truth for final counts.
- The counting source observes bytes read; it does not alter FileSystem or
  Uploader semantics.
- Log-file output is opt-in and does not replace normal console progress.

## Verification

Run sync unit tests and manual TTY/non-TTY checks when changing renderer behavior.
