# TODO

## Da fare quanto prima

- finire di pianificare il placeholder {info} nella caption
- formalizzare l'uso dei plugin con gli hook necessari
- impostare l'upload per processare i file in ordine di peso (dal piu' piccolo al piu' grande)
- creare il plugin che scrive nelle {info}
- modificare template caption nel file config.yaml
- bonificare tutte le caption su telegram

## Mirror Follow Ups

- Consider an explicit option to create the missing destination root for
  `mirror`; the first implementation intentionally fails clearly when the
  destination folder does not already exist.
- Consider a Web UI entry point for the existing virtual-to-virtual mirror
  workflow.

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
- Render only `nodes.info["notes"]` below the canonical `fileName:` caption line.
- Keep `info.notes` free-form: allow long text, line breaks, and empty lines;
  do not trim or silently truncate it. If Telegram rejects the caption, surface
  the error.
- Add a centralized caption renderer and update upload, rename, copy/move,
  merge, reorder, split, sanitizer, and recovery tooling to use it.
- Add a core helper to update `info.notes` and resync Telegram captions for
  Telegram-backed files.
- Add a plugin manager with ordered hook chains and a single public
  `PluginError` exception type.
- Add initial file-level hooks for upload, move, copy, rename, delete, and
  import.
- Wire the plugin manager through every `FileSystem` construction path,
  including HTTP, WebDAV, CLI sync, CLI fs operations, watcher/import-channel,
  and `FsExecutor`.
- Add an `install_plugin` or `plugins` command flow to make local plugin code
  available and validate configured plugin modules.
