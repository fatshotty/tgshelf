# TODO

## Mirror Follow Ups

- Consider an explicit option to create the missing destination root for
  `mirror`; the first implementation intentionally fails clearly when the
  destination folder does not already exist.
- Consider a Web UI entry point for the existing virtual-to-virtual mirror
  workflow.

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
- Include the current logical full path in Telegram captions, in addition to
  the filename, so disaster recovery can rebuild both file names and tree
  placement from channel history.
- Consider a dedicated configuration section for Telegram caption rendering.
- Support configurable caption placeholders, for example `{file_id}`,
  `{filename}`, `{path}`, `{channel_id}`, `{part_idx}`, `{parts}`, `{size}`,
  and `{mime}`, so operators can decide which recovery metadata is written into
  Telegram captions.
- Add a migration/reconciliation command to detect stale captions and repair
  them from the database.

## Flood Logging

- Verify that normal tgshelf runtime paths always log Telegram flood waits with
  the `[flood]` marker. The one-shot caption sanitizer may surface a different
  behavior during the historical cleanup, but regular service operations must
  keep flood/cooldown events visible in logs.

## Plugin Hooks And info.notes Captions

- Implement the design recorded in `docs/dev/plugins-and-info-notes.md`.
- Support editing text content above `telegram.upload.min_size` by converting or
  replacing it as Telegram-backed content. The first composite edit endpoint
  intentionally returns 409 for oversized inline text edits.
- Extend plugin work on top of the implemented `nodes.info["notes"]` field.
  Notes are rendered by `{info}` and capped at 200 characters.
- Add a plugin manager with ordered hook chains and a single public
  `PluginError` exception type.
- Add initial file-level hooks for upload, move, copy, rename, delete, and
  import.
- Wire the plugin manager through every `FileSystem` construction path,
  including HTTP, WebDAV, CLI sync, CLI fs operations, watcher/import-channel,
  and `FsExecutor`.
- Add an `install_plugin` or `plugins` command flow to make local plugin code
  available and validate configured plugin modules.
