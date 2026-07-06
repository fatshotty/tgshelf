# TODO

## Backup / Mirror Command

- Implement a first-class virtual-to-virtual backup/mirror workflow, exposed from
  the CLI, for example:
  `tgshelf --config config.yaml mirror /media/movies /backup/movies-bk-1`.
- The command must synchronize a source folder into a destination folder without
  requiring a local download/re-upload round trip.
- Preserve the latest active source tree in the destination by creating missing
  folders, copying new files, replacing changed files, and marking destination
  files/folders that no longer exist in the source as `DELETED`.
- Reuse existing server-side copy/move primitives where possible, but implement
  mirror-specific planning instead of relying on a one-shot `cp`.
- Support a safe dry-run/report mode that lists planned creates, updates,
  deletes, skips, and mismatches before mutating the destination tree.
- Add an optional cleanup step for mirror discards that purges only `DELETED`
  nodes under the backup root, using the existing `deleted_only` purge behavior.
- Add focused tests for idempotent re-runs, renamed files, changed file sizes,
  missing source entries, nested folders, and channel inheritance across the
  backup destination.

## Batch Delete / Purge CLI

- Allow the `delete`/`rm` and `purge` CLI commands to accept multiple file or
  folder paths in a single process run, for example:
  `tgshelf purge /media/a.mkv /other/folder/b.mkv /archive/old`.
- This avoids restarting the tool once per path when the operator needs to
  delete or purge several unrelated nodes, which currently causes repeated
  startup/login work.
- Resolve every requested path before mutating data, report missing paths
  clearly, and define whether the command should fail-fast or continue with
  the remaining valid paths.
- Keep logs grouped and readable per target path, especially for purge operations
  that delete Telegram-backed parts.

## Telegram Caption Consistency

- Track and implement Telegram caption updates when a Telegram-backed file is
  renamed in tgshelf.
- Current upload captions store the original physical part names, for example:
  `filename: Inception (2010) - WebDL 1080p AC3 10.1 GB.mkv.001`.
- A later tgshelf rename updates the database node name only; the Telegram
  message captions for the uploaded parts remain unchanged.
- This makes Telegram channel re-indexing/disaster recovery unsafe: rebuilding
  the database from channel history would recover stale names and could create
  serious inconsistencies with the latest logical filesystem state.
- Define a stable caption format that contains both immutable identity metadata
  and the current logical filename, then update captions on rename operations.
- Add a migration/reconciliation command to detect stale captions and repair
  them from the database.

## Flood Logging

- Verify that normal tgshelf runtime paths always log Telegram flood waits with
  the `[flood]` marker. The one-shot caption sanitizer may surface a different
  behavior during the historical cleanup, but regular service operations must
  keep flood/cooldown events visible in logs.

## Web UI Text Editing

- Fix the Web UI text-file editing flow. `.nfo` files are text files and must be
  editable as text.
