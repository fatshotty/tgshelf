# Repository Guidelines for Agents

## Scope

These instructions apply to the whole repository.

This project is `tgshelf`: a Python 3.12+ rewrite of Telegram-backed cloud
storage with PostgreSQL, Telethon, aiohttp, Alembic, CLI commands, and a Vite
React Web UI.

## Required Context

Before changing behavior, read these files:

- `README.md` for the short project overview.
- `docs/CLAUDE.md` for current status, implementation conventions, and open work.
- `docs/PLAN.md` for approved architecture and domain decisions.
- The relevant `docs/dev/*.md`, `docs/specs/*.md`, or `docs/plans/*.md` file for
  the area you are touching.

Treat the decisions in `docs/PLAN.md` and `docs/CLAUDE.md` as binding unless the
user explicitly approves a change.

## Boundaries

- Modify only this repository unless the user explicitly asks otherwise.
- The legacy code is read-only reference material. It may be reachable through
  `legacy/` or `~/Sites/telegram-stream/python/`; do not write to it.
- Do not write to the production legacy MongoDB or legacy Telegram session files.
  They are only read during migration-related work.
- Do not commit, push, or create pull requests unless the user explicitly asks.
  The user normally reviews and commits completed points.
- Keep unrelated refactors out of task work.

## Language

- Write code comments, logs, exceptions, diagnostics, commit messages, branch
  notes, and generated developer documentation in English.
- User-facing product copy may follow an existing intentional product language;
  otherwise prefer English.
- If you touch an area that already contains Italian comments or log messages,
  tell the user before final confirmation.

## Architecture Notes

- Core package: `src/tgshelf/`.
- Database layer: `src/tgshelf/db/`, SQLAlchemy async, Alembic migrations in
  `migrations/`.
- Telegram layer: `src/tgshelf/telegram/`, with Telethon hidden behind gateway
  protocols and fakes for tests.
- Domain facade: `src/tgshelf/core/fs.py`. HTTP, CLI, watcher, sync, and future
  mount integrations should converge through the FileSystem facade.
- HTTP layer: `src/tgshelf/http/`, aiohttp, JSON API under `/api/v1`, streaming
  routes at `/download/...`, WebDAV at `/dav`.
- CLI commands: `src/tgshelf/commands/`, registered from `src/tgshelf/cli.py`.
- Web UI source: `webui/`; built static assets are served from
  `src/tgshelf/webui/static/`.

## Development Rules

- Schema changes must use Alembic migrations. Do not use `create_all()` for
  application schema.
- Keep Telegram-specific details behind `telegram/gateway.py` and fakeable
  interfaces where possible.
- Use `FakeGateway` and local fakes for automated tests. Do not require real
  Telegram network access in normal unit or integration tests.
- Preserve the agreed domain rules: stable 10-character node IDs, soft delete,
  case-insensitive active sibling names, effective channel inheritance,
  server-side copy/move semantics, and `/download/{file_id}` path resolution by
  file id only.
- For new engines or operational flows, add grep-friendly log markers consistent
  with the existing style, such as `[sync]`, `[download]`, `[stream]`, `[watch]`,
  `[notify]`, `[migrate]`, `[flood]`, or a clear new marker.
- Every implementation task should leave a short technical note in
  `docs/dev/<task>.md` unless the user says the change is too small for one.

## Verification

Use the narrowest reliable checks first, then broaden when risk warrants it.

Python:

```sh
python -m pytest -q
python -m pytest tests/unit -q
python -m pytest tests/integration -q
```

If the local virtualenv is active or preferred:

```sh
.venv/bin/python -m pytest -q
.venv/bin/alembic history
.venv/bin/alembic upgrade head
```

Web UI:

```sh
cd webui
npm run typecheck
npm run build
```

Use `scripts/smoke.py` only for manual checks that truly need real Telegram
accounts/channels.

## Review Focus

When reviewing code in this project, prioritize:

- Data-loss or corruption risks in file moves, copies, upload resume, purge,
  merge, reorder, and migration code.
- Async session lifetime and transaction boundaries.
- Telegram flood/cooldown, failover, file reference, and channel eligibility
  behavior.
- HTTP streaming correctness: Range, disconnects, backpressure, HEAD/GET parity,
  ETag/304/416 behavior.
- API/OpenAPI drift.
- Web UI behavior that depends on backend contract changes.
- Missing tests around new domain rules, migrations, and error paths.

## Current Direction

At the time this file was created, documented next areas include Web UI upload,
Web UI parts management, Docker/compose runbook work, and several future
hardening items called out in `docs/CLAUDE.md` and `docs/PLAN.md`. Re-check those
documents before starting evolutionary work because they are the source of truth.
