# TODO

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
