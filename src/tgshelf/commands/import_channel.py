"""`tgshelf import-channel` — reconcile the master channel into the drive.

The watcher bot (C5.2) only catches LIVE messages; history and any downtime gap
are reconciled here. Bots cannot read channel history, so this runs through a
USER account: it scans the master channel and imports every file message missing
from the DB into root.

Idempotent: `fs.import_message` dedupes by (channel, message_id), so a re-run only
adds what is new. The same `reconcile` is called at watcher startup to cover the
window while the watcher was down.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, AsyncIterator

from tgshelf.config import Config
from tgshelf.constants import ROOT_ID
from tgshelf.core.fs import FileSystem
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.db.repo import NodeRepo

log = logging.getLogger("tgshelf.import")


@dataclass
class ImportStats:
    imported: int = 0
    skipped: int = 0

    def __str__(self) -> str:
        return f"{self.imported} imported, {self.skipped} skipped"


def _has_file(message: Any) -> bool:
    """A channel message we care about carries media (we catalog only files)."""
    return getattr(message, "media", None) is not None


async def reconcile(
    messages: AsyncIterator[Any],
    fs: FileSystem,
    channel_id: int,
    *,
    parent_id: str = ROOT_ID,
) -> ImportStats:
    """Import every file message in `messages` missing from the DB into
    `parent_id` (root for the master channel). `messages` is an async iterator of
    Telegram message-likes (`.id`, `.media`) — telethon's `iter_messages` in the
    command, a scripted list in tests. Pre-checking get_file_by_message keeps the
    stats honest (import_message returns the existing node on dupes either way)."""
    stats = ImportStats()
    async for message in messages:
        if not _has_file(message):
            continue
        if await fs.repo.get_file_by_message(channel_id, message.id) is not None:
            stats.skipped += 1
            continue
        node = await fs.import_message(channel_id, message.id, parent_id=parent_id)
        if node is None:
            stats.skipped += 1  # media we cannot catalog (e.g. non-document)
        else:
            stats.imported += 1
            log.info("[import] %s (msg %s)", node.name, message.id)
    return stats


async def run(config: Config, args) -> int:
    master = config.telegram.upload.channel
    limit = getattr(args, "limit", None) or None  # 0/None -> whole history

    from tgshelf.http.serve import make_write_limiter, start_clients

    write_limiter = make_write_limiter(config.operations)
    pairs = await start_clients(config, write_limiter)
    user = next(((acc, client) for acc, client in pairs if not acc.is_bot), None)
    if user is None:
        for _account, client in pairs:
            await _disconnect(client)
        print("error: a user account is required to read channel history", file=sys.stderr)
        return 1

    _account, gateway = user
    raw = gateway._client  # underlying telethon, for high-level iter_messages
    engine = create_engine(config.db)
    try:
        async with create_session_factory(engine)() as session:
            fs = FileSystem(
                NodeRepo(session),
                master_channel=master,
                gateway=gateway,
                min_size=config.telegram.upload.min_size,
                caption_template=config.caption.template,
            )
            log.info("[import] scanning master channel %s (limit=%s)", master, limit or "all")
            stats = await reconcile(raw.iter_messages(master, limit=limit), fs, master)
            log.info("[import] done: %s", stats)
    finally:
        await engine.dispose()
        for _account, client in pairs:
            await _disconnect(client)

    print(f"import-channel: {stats}")
    return 0


async def _disconnect(client: Any) -> None:
    disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
    if disconnect is not None:
        await disconnect()
