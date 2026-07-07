# tgshelf

Telegram as real cloud storage: a virtual filesystem backed by Telegram
channels, with HTTP upload/download endpoints for files of any size. Multi-GB
files are split into parts and reassembled transparently.

`tgshelf` stores file payloads in Telegram channels and keeps filesystem
metadata in PostgreSQL. It exposes folders and files through the Python CLI,
aiohttp APIs, HTTP download endpoints, WebDAV/rclone, and a React Web UI.

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
- Optional live watcher that imports files posted to the master channel while
  the server is running.
- Observability through `/status`, `/metrics`, `/metrics.txt`, Web UI SSE
  metrics, structured logs, and optional Telegram notifications.

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

## Development Setup

Create a virtual environment and install the Python package in editable mode:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Create and edit the runtime configuration:

```sh
cp config.example.yaml config.yaml
```

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

The Docker image contains only `tgshelf` and the built Web UI. PostgreSQL is
external and is configured through the `DB` environment variable.

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

For Docker and Portainer deployments, set these values in the mounted
`config.yaml`:

```yaml
data: /data

http:
  enabled: true
  host: 0.0.0.0
  port: 3000
```

On `serve`, the container runs `alembic upgrade head` before starting the HTTP
server. Set `TGSHELF_RUN_MIGRATIONS=0` if migrations are handled externally.

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

Release tags are created only from `main`, which is the official release branch.
Use semantic versions with a `v` tag prefix:

```text
v0.1.0
v0.1.1
v0.2.0
```

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

  # Optional watcher: a dedicated bot, separate from telegram.users bots.
  # It must be an admin of the master channel. It only imports files posted
  # while `serve` is running.
  main_bot:
    api_id: 123456      # dummy
    api_hash: "0123456789abcdef0123456789abcdef"  # dummy
    bot_token: "987654321:AAyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"  # dummy

  notify:
    # Optional Bot HTTP API sender for CRITICAL/ERROR notifications.
    # Empty = local logs only.
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
  # {info} is reserved and not implemented yet.
  # See docs/telegram-captions.md for full semantics.
  template: |
    fileName: {filename}

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

`telegram.main_bot` is a dedicated watcher bot, not one of the accounts under
`telegram.users`. It imports only files posted to the master channel while
`serve` is running.

The `DB` environment variable overrides the `db` key, which is useful for
deployment-specific database URLs.

## CLI Examples

All commands accept `--config`; the default is `./config.yaml`.

```sh
# Inspect configured accounts and saved sessions.
tgshelf --config config.yaml accounts list

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

# Start the HTTP API, Web UI, watcher, metrics, and enabled WebDAV surfaces.
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
