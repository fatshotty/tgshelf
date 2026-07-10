# tgshelf 1.0.0 Release Notes

This document is the tracked release and production handoff note for tgshelf
1.0.0. Local development notes under `docs/dev/` are intentionally not tracked.

## Release Scope

tgshelf 1.0.0 is the first production release of the Python rewrite. It provides:

- PostgreSQL-backed virtual filesystem metadata with Alembic-managed schema.
- Telegram-backed multipart file storage with inline database storage for small files.
- aiohttp JSON API, streaming download endpoint, WebDAV endpoint, and React Web UI.
- CLI workflows for account setup, filesystem operations, sync, download, STRM generation,
  bot checks, channel import, and operational maintenance scripts.
- Multi-account upload and multi-bot streaming with cooldown and failover handling.
- Operational endpoints for `/ping`, `/status`, `/metrics`, `/metrics.txt`, and Web UI SSE metrics.

## Production Preconditions

Before cutting the tag, verify:

- `develop` has been rewritten to remove local operational report artifacts from Git history.
- `develop` is merged into `main` intentionally after reviewing any commits that exist only on `main`.
- Package, CLI, Web UI, and OpenAPI versions agree on `1.0.0`.
- No stable tag newer than `v1.0.0` is used for the first official release.
  Stable tags are created from `main` only; beta tags are created from
  `develop` only by explicit manual release decision.
- The production `config.yaml` is outside Git and contains real Telegram and database values.
- PostgreSQL is reachable from the production host or container.
- At least one user account has a valid stored session.
- Download bots are registered and have access to the channels they must serve.
- The mounted data directory is writable by the runtime user.

## Verification Checklist

Run these checks from a clean checkout before tagging:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src scripts
.venv/bin/python -m pip check
.venv/bin/alembic history
cd webui
npm ci
npm run typecheck
npm run build
```

Run the Alembic upgrade check against a disposable or production-approved database:

```sh
TGSHELF_CONFIG=/path/to/config.yaml .venv/bin/alembic upgrade head
```

## Release Procedure

1. Confirm `develop` is clean and contains the final release commit.
2. Set the package version for the official first release to `1.0.0`.
3. Merge `develop` into `main`.
4. Run the verification checklist on `main`.
5. Create the only stable first-release tag from `main`:

   ```sh
   git tag -a v1.0.0 -m "Release v1.0.0"
   ```

6. Push `main` and the release tag only after review:

   ```sh
   git push origin main
   git push origin v1.0.0
   ```

Do not push `develop` as part of the first official release unless explicitly
requested.

## Versioning After 1.0.0

`main` carries stable public versions only:

```text
v1.0.0
v1.0.1
v1.0.2
```

`develop` carries the next patch line as optional beta snapshots:

```text
v1.0.1-beta1
v1.0.1-beta2
v1.0.1-beta5
```

Beta tags are never automatic. Create them from `develop` only when the operator
explicitly decides to freeze a test build. While `main` is frozen at `1.0.0`,
`develop` prepares `1.0.1-betaN` builds. When `main` later freezes `v1.0.1`,
`develop` moves to `1.0.2-beta1`, and the same pattern repeats.

Python packaging accepts versions such as `1.0.1-beta1` and normalizes them to
PEP 440 form (`1.0.1b1`) during builds. Git tags should keep the readable
`v1.0.1-betaN` form.

## Operational Notes

- The Docker entrypoint runs `alembic upgrade head` before `serve` unless
  `TGSHELF_RUN_MIGRATIONS=0` is set.
- `/download/{file_id}` resolves by file id only; decorated path segments are ignored.
- WebDAV is available under `/dav` when enabled in configuration.
- `/ping` is unauthenticated; other HTTP surfaces use Basic auth unless disabled or bypassed by CIDR.
- `scripts/check_integrity.py` is the recommended pre-cutover integrity check. Telegram-level
  verification requires real accounts and should be scheduled with Telegram limits in mind.

## Known Accepted Risk

The Web UI dependency audit currently reports Vite/esbuild advisories in development tooling.
This is accepted for the 1.0.0 preparation pass and is not fixed in this release prep.
