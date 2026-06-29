# CLI Download Design

## Goal

Add `tgshelf download <drive-path> [local-destination]`, the inverse of `sync`.
It downloads a single file or a whole subtree from the virtual drive to local
disk through `FileSystem.open_read()`.

## Behavior

- A file downloads to `DEST/<filename>`.
- A folder downloads as a mirrored subtree under `DEST/<folder-name>/`.
- Empty directories are created only when needed by files.
- Existing complete files with matching size are skipped.
- Existing shorter files are resumed from the local size.
- Existing longer or mismatched files fail unless overwrite is requested.
- Failures are collected per file; one failed file does not abort the batch.

## Progress And Logging

The command uses the shared progress renderer:

- TTY: live multi-line progress with per-file state and aggregate throughput.
- non-TTY: line-oriented progress suitable for logs.
- final recap: counts, bytes, elapsed time, and average rate.
- optional error log: append-only file with run header, file errors, and footer.

## Boundaries

- Planning is DB-only.
- Download bytes use the same stream path as HTTP download.
- Telegram behavior remains behind gateway and pool abstractions.
- Real Telegram performance is smoke-tested, not unit-tested.

## Verification

- Unit tests cover planning, skip/resume/overwrite decisions, progress rendering,
  recap formatting, and error-log formatting.
- Integration tests cover FileSystem-backed download with fakes.
- Manual smoke checks cover real Telegram throughput and byte correctness.
