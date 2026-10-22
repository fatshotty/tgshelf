"""`tgshelf import-channel` — reconcile the master channel into the drive.

The watcher bot (C5.2) only catches LIVE messages; history and any downtime gap
are reconciled here. Bots cannot read channel history, so this runs through a
USER account: it scans the master channel and catalogs supported document
messages into root, subject to the shared import name-collision rules.

`fs.import_message` checks for existing (channel, message_id) references before
importing. Reconciliation is manual: the watcher does not call it on startup.
There is no persistent scan checkpoint; --limit counts the latest messages, not
only supported files. See docs/watcher-import.md for collision and counter limits.
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
    """Media prefilter; the gateway later rejects unsupported media types."""
    return getattr(message, "media", None) is not None


async def reconcile(
    messages: AsyncIterator[Any],
    fs: FileSystem,
    channel_id: int,
    *,
    parent_id: str = ROOT_ID,
) -> ImportStats:
    """Catalog supported documents from `messages`, subject to reference/name
    checks, into `parent_id` (root for the master channel). `messages` is an async
    iterator of Telegram message-likes (`.id`, `.media`) — Telethon's
    `iter_messages` in the command, a scripted list in tests. The pre-check counts known
    messages as skipped; an active same-name node returned by import_message is
    still counted as imported, even though no new reference was recorded."""
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

    from tgshelf.http.serve import build_runtime, make_write_limiter, start_clients

    write_limiter = make_write_limiter(config.operations)
    pairs = await start_clients(config, write_limiter)
    engine = create_engine(config.db)
    try:
        from tgshelf.core.access import AccessRequirement
        sessions = create_session_factory(engine)
        runtime = build_runtime(config, sessions, pairs, write_limiter=write_limiter)
        async with runtime["admission"].start([AccessRequirement(master, "read")]) as ctx:
            raw = ctx.member.client._client
            async with sessions() as session:
                fs = FileSystem(NodeRepo(session), master_channel=master,
                    gateway=ctx.gateway, min_size=config.telegram.upload.min_size,
                    caption_template=config.caption.template, admission=runtime["admission"])
                fs._operation_context = ctx
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
