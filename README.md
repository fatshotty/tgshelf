# tgshelf

Telegram as a real cloud storage: a virtual filesystem (folders/files) backed by
Telegram channels, with an HTTP proxy for upload, download and streaming of
files of any size (multi-GB files are split into parts and reassembled
transparently).

Complete rewrite of [tgmanager](https://github.com/fatshotty/tgmanager).

## Status

Early development — see `docs/` for the design notes.

- **Stack**: Python 3.12+, Telethon, PostgreSQL (SQLAlchemy 2.0 async + Alembic), aiohttp
- **Key features (planned)**: multi-bot parallel downloading, per-folder Telegram
  channels, channel watcher (files posted to the master channel are catalogued
  automatically), changes feed for realtime consumers (rclone mount, .strm
  generation), migration from the legacy MongoDB database.


