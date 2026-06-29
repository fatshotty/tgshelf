# Legacy Knowledge Porting Notes

This document records the legacy behaviors that matter for the rewrite. The
legacy repository and data stores are read-only reference material.

Tags:

- **PORT**: preserve behavior in the rewrite.
- **FIX**: preserve the intent but repair a known legacy weakness.
- **DROP**: intentionally not carried forward.
- **TEST**: cover through unit, integration, or smoke checks.
- **FUTURE**: known follow-up outside the current implementation.

## Filesystem Semantics

- **PORT**: preserve stable 10-character node ids because generated `.strm`
  files and URLs embed them.
- **PORT**: preserve soft delete and explicit purge.
- **PORT**: preserve case-insensitive active sibling name uniqueness.
- **PORT**: preserve effective channel inheritance: nearest ancestor with a
  channel wins, otherwise the configured master channel is used.
- **PORT**: preserve root/master-channel mapping.
- **FIX**: keep path resolution in PostgreSQL recursive CTEs instead of repeated
  legacy graph lookups.
- **FIX**: keep all integrations behind the FileSystem facade instead of
  duplicating filesystem logic across HTTP, CLI, watcher, sync, and mount paths.

## Telegram Payloads

- **PORT**: treat every uploaded or imported media item as a generic document for
  storage purposes. Do not rely on Telegram media-specific transformations.
- **PORT**: split large files into multiple Telegram messages/portions.
- **PORT**: keep per-part channel/message/document metadata.
- **FIX**: verify `doc_id` before destructive cross-channel operations when the
  live Telegram message can be inspected.
- **FIX**: make interrupted cross-channel moves inspectable and recoverable by
  storing `channel_id` per part.
- **TEST**: cover missing part, document mismatch, and partial move/copy behavior
  with fakes.

## Upload

- **PORT**: use user accounts for upload and management operations.
- **PORT**: store files below `telegram.upload.min_size` inline in PostgreSQL.
- **PORT**: infer premium/free upload limits from account capabilities.
- **FIX**: if premium status is stale at runtime, clean up partial Telegram
  messages, mark the account free, notify, and retry with the free boundary.
- **FIX**: persist completed portions promptly so a crash does not make already
  finalized Telegram documents invisible to recovery checks.
- **TEST**: cover inline threshold, chunking, abort cleanup, premium downgrade,
  and retry behavior with `FakeGateway`.

## Download And Streaming

- **PORT**: use bot accounts for download and streaming.
- **PORT**: support HTTP Range semantics required by players and rclone.
- **FIX**: replace legacy sequential or ad hoc download behavior with
  `StreamPlan` and `ParallelStreamer`.
- **FIX**: keep chunk output ordered even when chunks are fetched by multiple
  bots.
- **FIX**: fail over when a bot floods, times out, loses file reference, or
  becomes channel-ineligible.
- **TEST**: cover range math, ordering, failover, bot exhaustion, user fallback,
  HEAD/GET parity, 304, and 416.

## Telegram Floods And Account Health

- **PORT**: treat FloodWait as an operational condition, not an unhandled bug.
- **FIX**: normalize flood waits into cooldown state in pools.
- **FIX**: add proactive per-account rate limiting before Telegram floods the
  account.
- **FIX**: keep receive updates disabled for pooled user/bot clients so shared
  sessions can work in the deployment model already proven by the legacy system.
- **TEST**: cover cooldown, quarantine, eligibility, least-loaded leasing, and
  `lease_or_wait`.

## Watcher And Import

- **PORT**: support importing files posted directly in the master channel.
- **FIX**: restrict the watcher to the master channel only. Do not implement a DM
  listener unless explicitly approved later.
- **FIX**: watcher failure must not kill `serve`; it logs and notifies.
- **FUTURE**: automatic watcher reconciliation after downtime. For now, run
  `tgshelf import-channel` on demand.
- **TEST**: cover live-message filtering and idempotent import with fakes.

## Migration

- **PORT**: migrate files and folders from legacy MongoDB while preserving ids.
- **DROP**: do not migrate legacy sessions; accounts are configured again through
  tgshelf auth flows.
- **FIX**: keep migration as a standalone script, not an application command.
- **FIX**: map invalid or missing legacy part document ids to nullable values and
  let integrity checks report risky files.
- **TEST**: cover pure mapping and load idempotence; use real Telegram only for
  optional smoke/integrity checks.

## HTTP And API

- **DROP**: legacy HTTP endpoint compatibility.
- **PORT**: preserve streaming behavior needed by media players.
- **FIX**: use clean JSON API routes under `/api/v1`.
- **FIX**: use `/download/{file_id}` as the stable streaming route and ignore
  trailing path/query decoration.
- **FIX**: keep API/OpenAPI drift covered by tests.
- **TEST**: cover route coverage, schemas, domain-to-status mapping, upload,
  download, merge, and metrics behavior.

## CLI

- **PORT**: preserve the operational command set: account management, sync,
  `.strm`, bot management, filesystem operations, and import.
- **FIX**: make commands thin wrappers over FileSystem and composition helpers.
- **FIX**: keep binary content on stdout and prompts/progress/errors on stderr
  where relevant.
- **TEST**: cover pure command helpers and CLI argument registration.

## Web UI

- **FIX**: build the new React UI against the clean HTTP API instead of carrying
  legacy UI assumptions forward.
- **PORT**: preserve core operator workflows: browsing, stats, tree management,
  upload, and parts management.
- **FUTURE**: upload and parts management are the next UI areas.

## rclone And Mounting

- **FIX**: use WebDAV as the current mount data plane and PostgreSQL
  LISTEN/NOTIFY plus rclone rc as the cache invalidation control plane.
- **FUTURE**: a custom rclone backend can be considered if WebDAV becomes too
  limiting.

## Notifications

- **PORT**: serious operational failures should be visible outside logs.
- **FIX**: send alerts through Telegram Bot HTTP API, not through the pooled
  Telethon clients.
- **FIX**: use structured alert payloads and configurable line-based templates.
- **TEST**: cover log-only mode, send failures, worker queue behavior, warning
  dedupe, structured template rendering, and compatibility with `{message}`.

## Legacy Behaviors Intentionally Not Preserved

- Global singleton filesystem state.
- Pyrogram runtime and monkey patches.
- Hybrid Telethon-login/Pyrogram-runtime behavior.
- MongoDB as the active metadata store.
- Application schema creation through `create_all()`.
- Telegram DM listener behavior.
- Legacy HTTP route compatibility.

## Smoke Areas Requiring Real Telegram

These cannot be fully proven by fakes:

- actual FloodWait timing;
- DC migration behavior;
- file-reference expiry behavior;
- bot membership and admin rights;
- BotFather conversation behavior;
- premium/free upload limit enforcement by Telegram;
- real media playback seeking behavior.

Use `scripts/smoke.py` only when a task genuinely touches these boundaries.
