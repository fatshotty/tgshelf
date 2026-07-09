# TODO

## Mirror Follow Ups

- Consider an explicit option to create the missing destination root for
  `mirror`; the first implementation intentionally fails clearly when the
  destination folder does not already exist.
- Consider a Web UI entry point for the existing virtual-to-virtual mirror
  workflow.

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
