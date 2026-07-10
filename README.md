# tgshelf

Telegram as real cloud storage: a virtual filesystem backed by Telegram
channels, with HTTP upload/download endpoints for files of any size. Multi-GB
files are split into parts and reassembled transparently.

`tgshelf` stores file payloads in Telegram channels and keeps filesystem
metadata in PostgreSQL. It exposes folders and files through the Python CLI,
aiohttp APIs, HTTP download endpoints, WebDAV/rclone, and a React Web UI.

## Contents

- [Features](#features)
- [Stack](#stack)
- [Performance Notes](#performance-notes)
- [Telegram Channels Bound To Folders](#telegram-channels-bound-to-folders)
- [Telegram Accounts And Bots](#telegram-accounts-and-bots)
- [Development Setup](#development-setup)
- [Docker](#docker)
- [Developing Plugins](#developing-plugins)
- [Verification](#verification)
- [Versioning And Releases](#versioning-and-releases)
- [Configuration](#configuration)
- [CLI Examples](#cli-examples)
- [TODO list](#todo-list)

## Features

- PostgreSQL metadata for a virtual tree with stable 10-character node IDs.
- Telegram channels as the physical storage layer, with optional per-folder
  channel inheritance.
- Inline DB storage for small files and multipart Telegram storage for larger
  files.
- HTTP downloads through `/download/{file_id}` with Range support, HEAD/GET
  parity, ETag, 304, and 416 handling.
- Parallel downloads through multiple bots or user accounts, with failover,
  cooldowns, and optional user-account fallback.
- Filesystem operations: create folders, rename, move, copy, mirror, soft
  delete, restore, purge, search, recursive size, merge parts, split parts, and
  reorder parts.
- CLI workflows for accounts/sessions, filesystem operations, sync, download,
  `.strm` generation, and bot checks.
- Web UI for browsing, search, metrics, tree management, inline text editing,
  and Telegram-backed file-part management.
- WebDAV endpoint for rclone, plus optional rclone rc cache invalidation through
  the PostgreSQL changes feed.
- Trusted in-process Python plugin hooks for file upload, move, copy, rename,
  delete, and import workflows.
- Observability through `/status`, `/metrics`, `/metrics.txt`, Web UI SSE
  metrics, and structured logs.

## Stack

- Python 3.12+
- Telethon
- PostgreSQL
- SQLAlchemy async + Alembic
- aiohttp
- Vite + React

## Performance Notes

Telegram throughput depends on account type, datacenter, server network, and
Telegram-side limits. In local testing, parallel downloads can aggregate multiple
bots/accounts to increase effective throughput until the deployment reaches
Telegram or network limits.

## Telegram Channels Bound To Folders

tgshelf uses PostgreSQL to maintain the logical folder and file tree, and uses
pre-existing Telegram channels as the physical storage backend for file content.

The root folder `/` uses the main Telegram channel configured in:

```yaml
telegram:
  upload:
    channel: -1001234567890
```

Each folder can inherit the parent folder's channel or define its own Telegram
channel. New files uploaded into that folder are stored in the folder's
effective channel.

Example:

```text
/                 -> main channel
/photos           -> inherits the main channel
/photos/raw       -> channel A
/photos/raw/2026  -> inherits channel A
/documents/pdf    -> channel B
```

tgshelf does not create Telegram channels. Channels must already exist and must
be configured manually in Telegram before they are used by tgshelf.

tgshelf does not currently rebuild a channel's history. If a mapped Telegram
channel already contains files, those files are not cataloged automatically.
tgshelf manages only new files uploaded through tgshelf after the channel has
been configured.

Changing the channel associated with a folder affects new uploads, but it does
not automatically move files that were already stored. Existing Telegram file
parts remain in the channel where they were created.

## Telegram Accounts And Bots

tgshelf requires at least one Telegram user account. User accounts handle
read/write operations: uploads, copies, moves, deletes, and maintenance.

Bots are optional. When configured, they are used only for downloads and
streaming, so they may have read-only access to mapped channels.

Multiple user accounts and multiple bots can be configured at the same time.
tgshelf rotates them according to the operation type to distribute load and
reduce the risk of flood waits.

All user accounts must have read/write access to every mapped channel. Bots, if
present, must be able to read all mapped channels.

Without bots, downloads and streaming can use user accounts by enabling:

```yaml
download:
  allow_user_fallback: true
```

## Development Setup

Clone the repository, create a virtual environment, and install the Python
package in editable mode:

```sh
git clone <repo-url> tgshelf
cd tgshelf
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Create and edit the runtime configuration:

```sh
cp config.example.yaml config.yaml
```

PostgreSQL must be reachable before running migrations. Put the async PostgreSQL
DSN in `config.yaml`:

```yaml
db: postgresql+asyncpg://DB_USER:DB_PASS@DB_HOST:5432/DB_NAME
```

Alternatively, set the `DB` environment variable; it overrides the `db` key in
`config.yaml`.

Prepare the database:

```sh
alembic upgrade head
```

Build the Web UI when you want the Python app to serve the static assets:

```sh
cd webui
npm install
npm run build
```

Start the server:

```sh
tgshelf --config config.yaml serve
```

Open the Web UI at:

```text
http://127.0.0.1:3000/webui
```

The server redirects `/` to `/webui`; older `/b/...`, `/search`, and `/stats`
Web UI routes redirect to their `/webui/...` equivalents.

## Docker

The Docker image contains only `tgshelf` and the built Web UI. PostgreSQL is not
included: use an existing PostgreSQL service and provide its async SQLAlchemy
DSN through the `DB` environment variable.

The DSN format is:

```text
postgresql+asyncpg://DB_USER:DB_PASS@DB_HOST:5432/DB_NAME
```

### Docker Compose / Portainer

`docker-compose.yml` is ready for Portainer stacks and intentionally does not
start PostgreSQL. In Portainer, set these stack environment variables:

```text
TGSHELF_DB=postgresql+asyncpg://DB_USER:DB_PASS@DB_HOST:5432/DB_NAME
TGSHELF_CONFIG_DIR=/opt/tgshelf/config
TGSHELF_DATA_DIR=/opt/tgshelf/data
TGSHELF_PLUGIN_DIR=/opt/tgshelf/plugins
TGSHELF_HTTP_PORT=3000
TGSHELF_RUN_MIGRATIONS=1
```

`TGSHELF_DB` becomes the container `DB` environment variable and overrides the
`db` value in `config.yaml`. Use the PostgreSQL hostname that is reachable from
the tgshelf container, for example the service DNS name on a shared Docker
network or the host name/IP of an external PostgreSQL server.

Prepare the host directories:

```sh
mkdir -p /opt/tgshelf/config /opt/tgshelf/data /opt/tgshelf/plugins
cp config.example.yaml /opt/tgshelf/config/config.yaml
```

For Docker and Portainer deployments, set these values in the mounted
`config.yaml`:

```yaml
data: /data

http:
  enabled: true
  host: 0.0.0.0
  port: 3000
```

If you develop or deploy plugins in Docker, put plugin modules in
`TGSHELF_PLUGIN_DIR` and reference the container path in `config.yaml`:

```yaml
plugins:
  enabled: true
  paths:
    - /plugins
  modules:
    - my_plugin:MyPlugin
```

Deploy the stack from Portainer, or run it locally with Docker Compose:

```sh
TGSHELF_DB='postgresql+asyncpg://DB_USER:DB_PASS@DB_HOST:5432/DB_NAME' \
docker compose up -d --build
```

On `serve`, the container runs `alembic upgrade head` before starting the HTTP
server. Set `TGSHELF_RUN_MIGRATIONS=0` if migrations are handled externally.

Interactive account setup can be run with the same Compose stack:

```sh
docker compose run --rm tgshelf accounts setup
```

When using bind mounts, make sure `TGSHELF_DATA_DIR` is writable by container
UID `10001`.

### Single Container

Build the image:

```sh
docker build -t tgshelf:local .
```

Run it against an existing PostgreSQL container or service:

```sh
docker run -d \
  --name tgshelf \
  --restart unless-stopped \
  -p 3000:3000 \
  -e DB='postgresql+asyncpg://DB_USER:DB_PASS@DB_HOST:5432/DB_NAME' \
  -e TGSHELF_CONFIG=/config/config.yaml \
  -v /opt/tgshelf/config:/config:ro \
  -v /opt/tgshelf/data:/data \
  tgshelf:local
```

Interactive account setup can be run with the same image and mounted config:

```sh
docker run --rm -it \
  -e DB='postgresql+asyncpg://DB_USER:DB_PASS@DB_HOST:5432/DB_NAME' \
  -e TGSHELF_CONFIG=/config/config.yaml \
  -v /opt/tgshelf/config:/config:ro \
  -v /opt/tgshelf/data:/data \
  tgshelf:local accounts setup
```

When using bind mounts, make sure `/opt/tgshelf/data` is writable by container
UID `10001`.

## Developing Plugins

tgshelf plugins are trusted Python modules loaded in-process. They are not a
sandbox boundary, so enable only plugins you control. The public plugin API,
hook list, context fields, and `PluginHost` methods are documented in
`docs/plugins.md`.

For local development, create a plugin module in a directory outside tracked
application code, then point `config.yaml` at that directory:

```yaml
plugins:
  enabled: true
  paths:
    - ./plugin
  modules:
    - media_info:MediaInfoPlugin
```

Minimal plugin skeleton:

```python
class MediaInfoPlugin:
    async def after_file_upload(self, ctx):
        notes = await ctx.host.get_info_notes(ctx.node.id)
        if "Uploaded by plugin" in notes:
            return
        lines = [line for line in notes.splitlines() if line]
        lines.append("Uploaded by plugin")
        await ctx.host.set_info_notes(ctx.node.id, "\n".join(lines))
```

Plugins share the same Python environment as tgshelf. In a local venv, install
extra plugin dependencies into `.venv`. In Docker, build a custom image or
install dependencies in the deployment environment before enabling the plugin.

## Verification

Python:

```sh
python -m pytest -q
```

Web UI:

```sh
cd webui
npm run typecheck
npm run build
```

## Versioning And Releases

The package version is defined in one place:

```python
src/tgshelf/__init__.py
```

`pyproject.toml` reads the version dynamically from that file, and
`tgshelf --version` prints the same value.

Stable release tags are created only from `main`, which is the official release
branch. Test release tags may be created from `develop` only when the operator
explicitly decides to freeze a beta build.

Use semantic versions with a `v` tag prefix. Stable tags have no suffix:

```text
v1.0.0
v1.0.1
v1.0.2
```

Beta tags are reserved for manually selected `develop` snapshots and use the
next stable version plus a `-betaN` suffix:

```text
v1.0.1-beta1
v1.0.1-beta2
v1.0.1-beta5
```

The first official public release is `v1.0.0` from `main`. While `main` is
frozen at `1.0.0`, `develop` may prepare `1.0.1-betaN` builds. When `main`
later freezes `v1.0.1`, `develop` moves on to `1.0.2-beta1`, and so on. Beta
tags are never automatic; create them only by explicit manual release decision.

Python packaging accepts versions such as `1.0.1-beta1` and normalizes them to
PEP 440 form (`1.0.1b1`) during builds. Keep the human-readable `-betaN` suffix
for Git tags.

Branch policy:

- `main` is merge-only. Do not write feature code or create direct code commits
  on `main`.
- `develop` is the integration branch for normal development.
- Feature branches must be created from `develop` and named `feature/...`.
- Hotfix branches must be created from `main` and named `hotfix/...`.
- Update `main` only by merging `develop` for releases, or by merging a
  `hotfix/...` branch for emergency patch releases.

## Configuration

`config.yaml` supports environment-variable references in any scalar string.
Every `${VAR}` occurrence is replaced from `os.environ` before validation, so
values can be embedded in larger strings:

```yaml
db: "postgresql+asyncpg://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}"

telegram:
  users:
    - name: bot01
      api_id: "${TELEGRAM_API_ID}"
      api_hash: "${TELEGRAM_API_HASH}"
      bot_token: "${TELEGRAM_BOT_TOKEN}"
```

Missing variables fail startup with a `ConfigError` that names the YAML path.
The `DB` environment variable still overrides the `db` key entirely.

```yaml
# Example configuration: all sensitive values are dummy values.
# Do not use api_id, api_hash, bot_token, or channel as-is.

data: ./data            # local directory for file sessions and runtime state

# PostgreSQL DSN. The DB environment variable overrides this value.
db: postgresql+asyncpg://DB_USER:DB_PASS@DB_HOST:DB_PORT/DB_NAME

logger: info            # no | error | warn | info | debug

# Where Telegram sessions are stored:
#   db   = tg_sessions table, recommended for single-instance deployments
#   file = {data}/{name}.session, useful when each instance owns its sessions
session_storage: db

# TCP connections per Telegram client on the data path.
# 0 or 1 = standard behavior; 2 = conservative boost; above 3 is discouraged.
concurrent_tcp_connections: 1

telegram:
  users:                # user accounts and bots; bots include bot_token
    - name: main
      api_id: 123456    # dummy: replace with your Telegram api_id
      api_hash: "0123456789abcdef0123456789abcdef"  # dummy
    - name: bot01
      api_id: 123456    # dummy
      api_hash: "0123456789abcdef0123456789abcdef"  # dummy
      bot_token: "123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # dummy

  upload:
    # Files <= min_size bytes stay inline in the DB, not on Telegram.
    # Must be a multiple of 524288, the Telegram part size.
    min_size: 2097152
    # Master channel mapped to root "/". Dummy: replace with your -100...
    channel: -1001234567890

  # Reserved for a future channel watcher. The bot that listens to channel
  # messages and catalogs them automatically is not managed yet, so this block
  # is currently not used by the runtime.
  main_bot:
    api_id: 123456      # dummy
    api_hash: "0123456789abcdef0123456789abcdef"  # dummy
    bot_token: "987654321:AAyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"  # dummy

  notify:
    # Reserved for future Telegram notifications. Notifications are not managed
    # yet, so this block is currently not used by the runtime.
    bot_token:          # optional; dummy if set
    # Optional destination: numeric ID (-100.../-...) or @username.
    # Empty = the master channel configured above.
    channel:
    # Optional notification template. Lines with placeholders that have no value
    # are omitted, so fields can be reordered or removed without empty labels.
    template: |
      [tgshelf:{severity}] {title}

      Impact: {impact}
      Scope: {scope}
      File: {file_path}
      Node: {node_id}
      Part: {part_idx}
      Channel: {channel_id}
      Account: {account}
      Cause: {cause}
      Action: {action}
      Time: {time}
      Host: {host}
      Key: {key}
    warning_window: 300

caption:
  # template: complete Telegram caption template for Telegram-backed file parts.
  # Rendered for new uploads/copies and re-rendered only when an operation
  # changes data referenced by the template. Existing historical captions are
  # not rewritten automatically when this value changes.
  #
  # Set template: "" to disable tgshelf-managed Telegram captions.
  #
  # Placeholders:
  #   {id}         stable logical node id
  #   {path}       logical parent folder path, root = /, no filename included
  #   {filename}   logical part filename, e.g. Movie.mkv.001
  #   {part_idx}   1-based part index
  #   {parts}      total number of parts in the logical file
  #   {size}       current Telegram part size in bytes
  #   {mime}       node MIME
  #   {channel_id} physical Telegram channel id for this part
  #   {info}       nodes.info["notes"], editable from CLI/Web UI; max 200 chars
  # See docs/telegram-captions.md for full semantics.
  template: |
    fileName: {filename}

plugins:
  # Trusted Python plugins loaded in-process. Disabled means no plugin module is
  # imported. See docs/plugins.md for the public Host API and hook semantics.
  enabled: false
  # Extra import roots added before loading plugin modules.
  paths: []
  # Plugin classes or factories loaded in order with "module:attribute" syntax.
  # Hooks run in the same order.
  modules: []

download:
  multi_bot_download: 3         # parallel bots per download; 1 = sequential
  allow_user_fallback: false    # use user accounts if the bot pool is exhausted
  chunk_timeout: 6              # seconds without a chunk before replacing a bot
  # Estimated buffer soft threshold. 0 = disabled.
  memory_soft_limit: 0

operations:
  # Logical filesystem jobs that may run at once. Management jobs use user
  # accounts; downloads use the download bot/user pool settings above.
  concurrent: 4
  # Proactive per-account Telegram write budget. 0 actions disables it.
  # Token-bucket model: each account starts with `actions` write tokens. Every
  # flood-sensitive Telegram write (send/copy/delete/edit/admin action) consumes
  # one token; the bucket refills gradually from empty to full over `within`
  # seconds. When an account has no token, tgshelf uses another eligible account;
  # if every account is empty, it waits for the first token to refill.
  actions: 16
  within: 40            # seconds to refill a fully drained account bucket

http:
  enabled: true
  host: 127.0.0.1
  port: 3000
  user: ""              # empty = no basic auth
  pass: ""
  ignore_auth_for: []   # CIDRs without basic auth, e.g. ["192.168.1.0/24"]

strm:
  destination: ./strm-folder   # local directory where .strm files are generated
  source: /             # virtual folder used as the .strm generation root
  # Arbitrary template for .strm file content.
  # The path must start with /download/{file_id}; the rest is decorative.
  # Useful placeholders: {file_id}, {filename}, {channel_id}, {parts_dash},
  # {size}, {mime}.
  template: "http://127.0.0.1:3000/download/{file_id}/{filename}"
  clear_folder: false   # wipe the local directory before generating the .strm tree

changes_feed:
  enabled: false        # PostgreSQL trigger + LISTEN/NOTIFY
  retention_days: 7

# rclone integration: WebDAV data plane and rc bridge for cache invalidation.
rclone:
  webdav_enabled: false   # expose read-write WebDAV at /dav
  bridge_enabled: false   # LISTEN changes_feed -> vfs/forget
  # Shared secret used to register the rc endpoint through X-Tgshelf-Token.
  # Empty = self-registration disabled.
  register_token: "secret"
  # Additional CIDRs allowed for rc hosts declared by rclone clients.
  allowed_rc_networks: []
  registry_ttl: 600       # seconds before an idle mount is removed
```

Required values are the PostgreSQL DSN and `telegram.upload.channel`. At least
one user account is required for uploads and management operations. Bot accounts
are optional, but they are what make parallel downloads useful.

The `telegram.main_bot` and `telegram.notify` blocks are reserved for future
work. tgshelf does not yet manage a bot that listens to channel messages and
catalogs them automatically, and Telegram notifications are not wired yet.

The `DB` environment variable overrides the `db` key, which is useful for
deployment-specific database URLs.

## CLI Examples

All commands accept `--config`; the default is `./config.yaml`.

```sh
# Inspect configured accounts and saved sessions.
tgshelf --config config.yaml accounts list

# Validate saved user sessions and print live Telegram account caps, including
# Premium: True/False, max upload parts, DC, and user id.
tgshelf --config config.yaml accounts check
tgshelf --config config.yaml accounts check main backup

# Create every missing user and bot session from telegram.users.
tgshelf --config config.yaml accounts setup

# Recreate every user and bot session from telegram.users.
tgshelf --config config.yaml accounts setup --force

# Interactive login for one user account defined in telegram.users.
tgshelf --config config.yaml accounts login main

# Register one or more bots whose bot_token is already present in config.yaml.
tgshelf --config config.yaml accounts add-bot bot01
tgshelf --config config.yaml accounts add-bot bot01 bot02 bot03
tgshelf --config config.yaml accounts add-bot --all

# Start the HTTP API, Web UI, metrics, and enabled WebDAV surfaces.
tgshelf --config config.yaml serve

# Create folders in the virtual filesystem.
tgshelf --config config.yaml mkdir /folder/sub-folder

# Upload a local tree into the virtual filesystem.
tgshelf --config config.yaml sync ./folder-to-up --dest /folder/sub-folder [--concurrent 3] [--delete-source]

# List, search, measure, print, copy, move, soft-delete, and purge nodes.
tgshelf --config config.yaml ls /folder
tgshelf --config config.yaml search readme
tgshelf --config config.yaml du -H /folder/sub-folder
tgshelf --config config.yaml cat /notes/readme.txt
tgshelf --config config.yaml cp /notes/readme.txt /archive
tgshelf --config config.yaml cp --force-copy /notes/readme.txt /archive
tgshelf --config config.yaml cp /media/movies /backup
tgshelf --config config.yaml cp '/media/movies/*' /backup/movies-bk-1
tgshelf --config config.yaml cp '/media/movies/*' /archive/movies
tgshelf --config config.yaml mv /archive/readme.txt /folder/sub-folder
tgshelf --config config.yaml rm /notes/readme.txt
tgshelf --config config.yaml purge /notes/readme.txt

# Mirror one virtual folder into another. The destination root must already
# exist. Source contents win: missing entries are copied, changed entries are
# replaced, and destination-only entries are soft-deleted.
tgshelf --config config.yaml mirror /media/movies /backup/movies-bk-1
tgshelf --config config.yaml mirror --dry-run /media/movies /backup/movies-bk-1

# Download a file or folder. Existing partial files are resumed unless
# --overwrite is used explicitly.
tgshelf --config config.yaml download /archive/big-file.bin --dest ./restore [--concurrent 4]

# Generate .strm files from the virtual tree.
tgshelf --config config.yaml strm --source /folder --destination ./strm [--clear]

# Verify or repair bot membership on channels used by the filesystem.
tgshelf --config config.yaml bots check
```

Example rclone WebDAV remote:

```sh
rclone config create tgshelf webdav \
  url http://127.0.0.1:3000/dav \
  vendor other

rclone mount tgshelf: /mnt/tgshelf
```

## TODO list

- Manage the Telegram bot that listens to channel messages and catalogs them
  automatically.
- Wire Telegram notifications.
