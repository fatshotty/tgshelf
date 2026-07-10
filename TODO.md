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

## Telegram Caption Reconciliation

- Add a migration/reconciliation command to detect stale captions and repair
  them from the database. Runtime caption templates, placeholders, path-aware
  captions, and caption updates for new mutations are implemented; historical
  channel history still needs an explicit repair flow.

## Flood Logging

- Verify that normal tgshelf runtime paths always log Telegram flood waits with
  the `[flood]` marker. The one-shot caption sanitizer may surface a different
  behavior during the historical cleanup, but regular service operations must
  keep flood/cooldown events visible in logs.

## Plugin Follow Ups

- Support editing text content above `telegram.upload.min_size` by converting or
  replacing it as Telegram-backed content. The first composite edit endpoint
  intentionally returns 409 for oversized inline text edits.
- Wire the plugin manager into watcher and `import-channel` construction paths
  so `after_file_import` also runs for Telegram channel imports. HTTP/WebDAV,
  CLI sync, CLI fs operations, and `FsExecutor` are already wired.
