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

- `name`: optional deployment instance name; defaults to the local hostname and
  is exposed to notification templates as `{instance}` and to the Web UI through
  the authenticated `GET /api/v1/info` endpoint for the header and browser title.
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
  token bucket (`actions` over `within` seconds), also used explicitly by
  channel-import metadata reads.
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
- `channel_import_progress` (migration `0006`): exactly `folder_id`, `channel_id`,
  `last_scanned_message_id`, `updated_at`; compound folder/channel primary key,
  nonnegative monotonic cursor, folder FK with purge cascade. No item/run ledger.
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
- consecutive timeout failures and fixed-duration quarantine with lazy,
  generation-safe automatic recovery;
- per-channel eligibility.

Fetch results carry the member health generation captured before the Telegram
request. Results from an older generation are ignored, so concurrent work that
finishes late cannot shorten, extend, or reopen quarantine.

Leasing is weighted least-loaded with LRU tie breaks. `lease_or_wait` waits for
the earliest usable member instead of failing when every member is cooling down.

`AccessAdmission` shares one process-wide `operations.concurrent` file budget
between management operations and uploads. `FsExecutor` opens an independent DB
session per fan-out worker. Short DB-only policy snapshots select configured
accounts by containing-folder channel, never file/part channel. Copy requires
source read plus destination write; move requires write on both. Other mutations
require write, readers require read, and bots are never management writers.

PostgreSQL channel overrides (`none`, `read`, `write`) are edited only through the
authenticated Web UI API. Missing rows allow full control for users/read for bots;
root/master policy is non-editable even through channel aliases. Folder aliases
share overrides. Saves affect queued files/new streams, not admitted files, cleanup
or running streams. DB failures fail closed. Hard account masks survive cooldown,
quarantine, alternative-account preflight and stream fallback.

Preflight validates every message and known document ID with one eligible user;
access denial retries the complete file with another eligible user. All denied
means a whole-file skip. TEMP staging persists newly copied parts before final
publication. Moves retain IDs and commit before deleting originals. Whole-folder
moves are conservative all-descendants-success commit groups; recursive copy may
report partial completion, and purge retains failed members' references. Failed
cleanup preserves TEMP references or reports post-commit warnings; Telegram and
PostgreSQL cannot provide a shared atomic commit. Account-bound write gateways
retain the existing per-account pacing without choosing a different account.

## FileSystem Facade

`src/tgshelf/core/fs.py` is the domain API. Integrations should converge here
instead of duplicating filesystem rules.

Important operations:

- read/navigation: `get`, `resolve`, `path_of`, `list_children`, `walk`, `search`;
- tree writes: `mkdir`, `mkdirs`, `rename`, `set_channel`;
- content: `open_read`, `write`, `edit_inline_content`;
- Telegram imports: `import_message` for the watcher and
  `catalog_channel_file` for admitted normalized history metadata;
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
- Read preparation uses a single non-locking SELECT for metadata, inline content,
  ordered part references and the containing-folder channel. Download-only DB
  credentials need no row-lock/write privileges; mutation locks remain unchanged.

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
It starts only in `serve`, with `receive_updates=True` and an ephemeral token-based
session outside `telegram.users` and the pools. The operator adds it as an admin
of `telegram.upload.channel` manually. There is no separate watcher-channel setting.

It listens for new direct posts and forwards in the master channel and imports
supported Telegram document media into root through `fs.import_message`, using
an admitted user account for metadata resolution. The facade's import wrapper
requires root-channel read access and shares the `operations.concurrent` budget.
Native photos are skipped. Each
new file references one existing Telegram message; import does not transfer bytes,
apply the inline threshold, parse/edit captions, or reconstruct multipart files.

Existing channel/message references are returned unchanged, even for soft-deleted
nodes. A case-insensitive same-name active sibling suppresses the incoming import;
a returned deleted sibling is reactivated with the same ID and a replacement part.
The watcher lookup does not prioritize state or restrict siblings to files.
Watcher and CLI publication share a transaction advisory lock and duplicate
recheck for each physical channel/message. The CLI supplies its configured plugin
manager and runs `after_file_import` for newly committed files; the watcher does
not currently supply a plugin manager.

Files posted while the watcher is down are not automatically recovered.
`tgshelf import-channel [path]` catalogs the mapped folder's channel using only
read-authorized user accounts. It specializes the access model with fresh
admission for every history page and file publication; bots never scan history.
It shares `operations.concurrent` across scanning/publication and opts history
metadata attempts into the existing per-account actions/within and FloodWait
machinery. No Telegram wait holds a database transaction.

Ascending bounded pages use a startup snapshot of the latest message ID.
Completed pages advance the monotonic folder/channel checkpoint, including
filtered, unsupported and terminal-error items. Fatal history/DB failures and
cancellation leave the incomplete page for replay. Normal runs are incremental;
`--rescan` starts at zero and never reduces the stored checkpoint. `--limit`
limits examined messages oldest first, with zero meaning unlimited.

Repeatable CSV extension/MIME includes combine with OR; exclusions veto them.
Every supported document is an independent one-part file (idx=0). Native photos
are currently unsupported. New nodes use source-message ctime and insertion
mtime. Global channel/message dedupe includes existing multipart and deleted
owners; collisions between distinct messages use a message-ID name suffix.
Short per-message transaction advisory locks are shared with watcher imports.
A dedicated session advisory lock prevents concurrent runs for the same
folder/channel pair and requires a direct or session-pooling DB endpoint.
Per-file reason logs accompany a final summary; file/run errors return nonzero.

Watcher startup failures and unexpected terminal disconnections are logged and
notified without terminating `serve`. Individual message failures are logged only.
The health monitor does not recreate the watcher. Live imports are admitted
per event; the CLI releases its history-page admission before dispatching file
workers. See [the implementation guide](watcher-import.md) for setup, distinct
watcher/history collision rules and limitations.

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
