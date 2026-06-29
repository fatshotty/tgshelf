# CLI Download Implementation Plan

## Goal

Implement `tgshelf download` for files and folders, using the same read path as
HTTP streaming.

## Completed Tasks

- Add CLI parser entry and command wiring.
- Add planning helpers that resolve a drive path and enumerate file targets.
- Add local destination decision logic for file and folder downloads.
- Add skip, resume, overwrite, and sanity checks.
- Add concurrent worker execution controlled by `operations.concurrent`.
- Reuse the shared progress renderer for TTY and non-TTY output.
- Add optional append-only error log.
- Add final recap with counts, bytes, elapsed time, and average rate.
- Add tests for planning, local decisions, progress, and command wiring.

## Important Constraints

- File bytes come from `FileSystem.open_read()`.
- Inline files and Telegram-backed files use the same command path.
- Empty folders are not eagerly created.
- Partial files remain on disk so reruns can resume.
- Real Telegram throughput remains a smoke-test concern.

## Verification

Use targeted unit tests first, then broader CLI/FileSystem tests. Real Telegram
verification should compare downloaded bytes against a known source file.
