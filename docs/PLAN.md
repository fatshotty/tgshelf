# tgshelf Architecture Plan

This is the approved architecture for the rewrite. It documents the design that
the current code is expected to preserve.

## Goal

Build a standalone, maintainable replacement for the legacy Telegram-backed
storage tool:

- PostgreSQL for durable metadata and change notifications.
- Telethon for all Telegram MTProto work.
- A single FileSystem facade shared by CLI, HTTP, watcher/import, sync, Web UI,
  WebDAV, and future mount integrations.
- Multi-bot parallel download/streaming.
- Crash-aware upload, move, copy, purge, and migration flows.
- Operational observability through logs, metrics, status endpoints, and
  Telegram notifications.

## Non-Goals

- No compatibility promise for legacy HTTP endpoints.
- No writes to legacy MongoDB or legacy Telegram session files.
- No real Telegram dependency in normal unit or integration tests.
- No `create_all()` for application schema; schema changes use Alembic.

## Configuration

Configuration is YAML loaded by `src/tgshelf/config.py`.

Before typed validation, scalar strings in the YAML tree expand `${VAR}`
references from `os.environ`. This supports both whole-value references, such as
`bot_token: "${TELEGRAM_BOT_TOKEN}"`, and embedded references, such as
`db: "postgresql+asyncpg://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}"`.
Missing variables are startup configuration errors. The `DB` environment
variable remains a full override for the `db` key.

Important blocks:

- `db`: SQLAlchemy async PostgreSQL DSN.
- `session_storage`: `db` or `file`.
- `telegram.users`: user accounts and bot accounts.
- `telegram.upload.channel`: master channel mapped to root.
- `telegram.upload.min_size`: inline DB storage threshold.
- `telegram.main_bot`: optional dedicated watcher bot.
- `telegram.notify`: optional Bot HTTP API alert sender and line-based template.
- `download`: multi-bot settings, fallback policy, chunk timeout, memory soft
  limit.
- `operations`: logical job concurrency plus the per-account Telegram write
  token bucket (`actions` over `within` seconds).
- `http`: aiohttp listener and auth settings.
- `strm`: `.strm` generation settings.
- `changes_feed`: PostgreSQL change table/NOTIFY toggle.
- `rclone`: WebDAV and rc bridge settings.

## Database Model

Core tables:

- `nodes`: virtual filesystem entries. A node has a stable id, parent, name,
  folder flag, MIME, optional channel override, state, size, optional inline
  content, info JSON, and timestamps.
- `parts`: Telegram message references for file payload parts. Each part stores
  file id, index, channel id, message id, document id, size, and original file
  name.
- `tg_sessions`: account and bot session metadata plus stored StringSession when
  configured for DB-backed sessions.
- `upload_states`: best-effort upload resume state.
- `changes`: append-only change feed populated by PostgreSQL triggers.
- `operation_jobs` and `operation_job_items`: durable, best-effort Web UI bulk
  move/soft-delete requests and their per-node outcomes. Node IDs and target IDs
  are snapshots rather than foreign keys so operational history remains readable
  after a later purge.

Rules:

- Only root has no parent.
- Active sibling names are unique case-insensitively.
- `content IS NOT NULL` means the file is DB-inline.
- `parts.channel_id` is authoritative per part and allows interrupted
  cross-channel moves to remain inspectable and recoverable.
- Path and tree operations use recursive CTEs through `NodeRepo`; business logic
  stays in Python except for change-feed triggers.

## Telegram Layer

`src/tgshelf/telegram/gateway.py` is the only surface visible to engines. It
keeps Telethon types out of core logic and makes tests fakeable.

`TgClient` handles:

- raw upload/download calls;
- flood wait translation and bounded retry;
- file-reference refresh signaling;
- session-dead and channel-unavailable translation;
- proactive write-budget integration;
- optional multiple TCP senders per data path.

Errors are normalized in `src/tgshelf/telegram/errors.py` and carry severities
for logging and notifications.

## Pools And Concurrency

`ClientPool` manages user accounts for upload and management operations.
`BotPool` manages bot accounts for download and streaming.

Pool members track:

- in-flight work;
- cooldown deadlines;
- consecutive failures and quarantine;
- per-channel eligibility.

Leasing is weighted least-loaded with LRU tie breaks. `lease_or_wait` waits for
the earliest usable member instead of failing when every member is cooling down.

`FsExecutor` wraps filesystem operations that should be governed by
`operations.concurrent`. It opens an operation-local DB session for each unit of
work. Telegram management writes go through the write-aware user gateway, which
uses the per-account token bucket to choose an eligible account per write.

## FileSystem Facade

`src/tgshelf/core/fs.py` is the domain API. Integrations should converge here
instead of duplicating filesystem rules.

Important operations:

- read/navigation: `get`, `resolve`, `path_of`, `list_children`, `walk`, `search`;
- tree writes: `mkdir`, `mkdirs`, `rename`, `set_channel`;
- content: `open_read`, `write`, `edit_inline_content`;
- Telegram imports: `import_message`;
- composition: `merge_parts`, ordered part merge, and `split_parts`;
- mutations: `move`, `copy`, `delete`, `restore`, `purge`.

The facade owns transaction boundaries where practical and delegates Telegram
payload operations to upload, download, and channel helpers.

## Upload

Small files up to `telegram.upload.min_size` are stored inline in PostgreSQL.
Larger files are uploaded to Telegram:

- source bytes are re-chunked into Telegram part sizes;
- multiple `SaveBigFilePart` calls can be in flight;
- portions are finalized as Telegram documents;
- part rows are persisted as portions complete;
- abort cleanup removes finalized Telegram messages when possible;
- premium/free upload boundaries are derived from the leased account;
- if premium status is stale, the account is marked free, a notification is
  emitted, and upload retries with the free boundary.

## Download And Streaming

`StreamPlan` maps byte ranges over file parts and emits 1 MiB-aligned chunk
requests. `ParallelStreamer` leases fixed bots for a stream, fetches chunks in
parallel, emits bytes in order, and performs transparent failover.

Important rules:

- HTTP supports single Range requests, ETag/304, 416, HEAD/GET parity, and
  disconnect cleanup.
- `/download/{file_id}` resolves only by node id.
- Bot exhaustion emits warnings and either waits or falls back to user accounts
  depending on configuration.
- PartMissing is a critical integrity failure.

## HTTP And WebDAV

The aiohttp application provides:

- `/ping`;
- JSON API under `/api/v1`;
- streaming upload/download routes;
- `/status`;
- `/metrics` JSON and `/metrics.txt` text;
- SSE metrics stream for the Web UI;
- durable bulk-operation job endpoints under `/api/v1/jobs`;
- WebDAV at `/dav` when enabled.

Responses are JSON unless a route explicitly documents a text or streaming
format. Basic auth and CIDR bypass are shared across HTTP surfaces.

## CLI

The CLI is registered from `src/tgshelf/cli.py`. Implemented commands include:

- account/session commands, including live user-account checks for premium status
  and upload caps;
- `serve`;
- filesystem commands: `mkdir`, `ls`, `cat`, `du`, `search`, `cp`, `mirror`,
  `mv`, `rm`, `purge`;
- transfer commands: `sync`, `download`;
- `.strm` generation;
- bot creation, selective join, and full membership check/repair commands;
- `import-channel`.

Migration from MongoDB remains a standalone script, not a CLI subcommand.

`mirror` supports an optional `--max-hours` positive whole-number limit; an
omitted limit is unlimited. Each invocation replans from current source and
destination state, so scheduled reruns skip aligned files and account for source
additions, modifications, renames/moves, and soft deletions between runs. The
deadline stops new work from starting, drains the already-started batch, and may
therefore exceed the requested duration by that batch's finish time. Deadline
completion is a normal status-0 CLI result with `completed=false` and
`deferred=<n>`; transfer or action failures are reported and return non-zero.

Mirror file work runs at `operations.concurrent` through `FsExecutor` and the
shared account pool, retaining the established write rate-limit, cooldown, and
account-leasing rules. Changed files are staged with copy-then-swap; all mirror
deletions are soft deletes.

For a running CLI mirror, the first `SIGINT` or `SIGTERM` is cooperative: it
stops scheduling, drains the active batch, and returns a normal partial result.
A second signal force-cancels the command for emergency use. The first-signal
path is logged with `[mirror]` and emits `stopped_by_signal=true`; a forced
cancellation can leave an in-progress TEMP copy for the next run to retry.

## Watcher And Import

The watcher is an optional dedicated bot configured under `telegram.main_bot`.
It listens only to the master channel while `serve` is running and imports live
file messages into root through `fs.import_message`.

Files posted while the watcher is down are not automatically recovered.
`tgshelf import-channel` performs on-demand history reconciliation through a
user account because bots cannot reliably read channel history.

Watcher failures are never fatal to `serve`; they are logged and notified.

## Notifier

The Notifier is best-effort:

- logs synchronously with `[notify]`;
- sends through the configured Telegram alert channel when enabled;
- uses a background queue in `serve`;
- deduplicates WARNING events by event id and window;
- never raises into upload, download, watcher, or HTTP paths;
- supports structured payload templates with per-line omission for empty fields.

## Web UI

The Web UI lives in `webui/` and is served from
`src/tgshelf/webui/static/` after build. Current implemented areas:

- browse/search;
- stats via SSE metrics;
- tree management;
- inline editing for DB-inline files;
- parts management for Telegram-backed files: list parts, merge selected files
  with explicit part ordering, and split selected parts back into sibling files.
- durable bulk move and soft-delete actions from browse and search, plus a shared
  Operations page that shows progress and per-item failures/skips.

The backend and OpenAPI already expose file-parts management primitives:
`POST /api/v1/nodes/{id}/merge`, `GET /api/v1/nodes/{id}/parts`,
`POST /api/v1/nodes/{id}/parts/split`, and `PUT /api/v1/nodes/{id}/parts`. The
Web UI API client has matching methods, and the browse view exposes merge through
multi-selection plus split through each Telegram-backed file's row menu.

Planned next area is upload.

## rclone Integration

The rclone integration combines:

- WebDAV data plane through the FileSystem facade;
- self-registration of rclone rc endpoints through request headers;
- in-memory rc registry with TTL and anti-SSRF checks;
- PostgreSQL LISTEN/NOTIFY bridge that calls `vfs/forget` on registered rc
  endpoints.

This provides a read-write mount path and near-instant directory cache refresh.

## Logging And Observability

Use stable, grep-friendly markers. Existing markers include:

`[sync]`, `[download]`, `[stream]`, `[fetch]`, `[fetch-slow]`, `[buf]`,
`[looplag]`, `[watch]`, `[import]`, `[notify]`, `[migrate]`, `[flood]`,
`[quarantine]`, `[eligibility]`, `[recover]`, `[wait]`, `[exhausted]`,
`[ratelimit]`, `[premium]`, `[rcbridge]`, `[job]`, `[mirror]`, `[join]`.

`[mirror]` records planning and application summaries, including action counts,
deferred work at a time limit, and individual action failures. CLI summaries
expose `completed` and `deferred` so an operational scheduler can distinguish a
normal deadline exit from a failed transfer/action run.

New engines or operational flows should add similarly clear markers.

## Testing Strategy

- Unit tests use fakes, especially `FakeGateway`, and must not require real
  Telegram.
- Integration tests use PostgreSQL and aiohttp test utilities where needed.
- Real Telegram behavior is covered by manual smoke checks only.
- Web UI verification is `npm run typecheck` and `npm run build` unless a test
  runner is added later.

## Current Known Gaps

- Complete Docker/compose/cutover documentation is still pending.
- Web UI upload is pending.
- Full notifier event catalog coverage is incremental.
- Explicit recovery notifications for pool recovery are still pending.
- Automatic watcher reconciliation after downtime is still future work.
- Redis-based cross-instance rate coordination is only a prepared extension.
