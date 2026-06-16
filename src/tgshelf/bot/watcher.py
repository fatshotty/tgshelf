"""Channel watcher: a dedicated bot that catalogs files posted LIVE to the
master channel into root.

Runs ONLY inside `serve`, ONLY when `telegram.watcher` names a bot account. The
bot is a dedicated instance (`receive_updates=True`) kept OUT of the download
pool (pool clients are receive_updates=False to share sessions across instances;
the watcher needs updates). It reacts ONLY to live messages in the master channel
that carry a file → `fs.import_message(master, msg_id, root)`.

Scope limits (decisione utente):
- only the master channel, only messages with a file attached;
- files posted while the watcher is DOWN are NOT recovered automatically — run
  `tgshelf import-channel`. Startup auto-reconciliation is a future point (PLAN.md).

Bots cannot reliably read history/resolve documents, so the document is fetched
through a USER account gateway (as the legacy did). This is the Telegram/IO
boundary: smoke-tested, not unit-tested.
"""

from __future__ import annotations

import logging
from typing import Any

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from tgshelf.commands.import_channel import _has_file
from tgshelf.config import Config
from tgshelf.constants import ROOT_ID
from tgshelf.core.fs import FileSystem
from tgshelf.db.repo import NodeRepo
from tgshelf.telegram.errors import Severity

log = logging.getLogger("tgshelf.watcher")


async def start_watcher(
    config: Config, *, session_factory: Any, user_gateway: Any, notifier: Any = None
) -> TelegramClient | None:
    """Start the watcher bot and attach the live handler. Returns the connected
    client (so serve can disconnect it on shutdown), or None when the watcher is
    not configured/usable or fails to start.

    The watcher is NEVER fatal to `serve` (decisione utente): a missing config is
    a log warning; a real startup failure is logged AND pushed to the notify
    channel, and serve keeps running without live cataloging (`import-channel`
    backfills). `user_gateway` is a connected USER client used to fetch the doc.

    `main_bot` is a dedicated brand-new bot (its own token, outside the pool): it
    authenticates from the token alone, so there is no session to load from the
    store — an ephemeral StringSession + `start(bot_token=...)` is enough."""
    main_bot = config.telegram.main_bot
    if main_bot is None:
        return None
    if user_gateway is None:
        log.warning("[watch] needs a user account as gateway; watcher disabled")
        return None

    master = config.telegram.upload.channel
    try:
        client = TelegramClient(
            StringSession(), main_bot.api_id, main_bot.api_hash, receive_updates=True
        )
        await client.start(bot_token=main_bot.bot_token)
    except Exception as exc:  # noqa: BLE001 - watcher failure must NOT block serve
        msg = (
            f"main_bot watcher failed to start ({exc}); serving continues WITHOUT "
            "live cataloging — run `tgshelf import-channel` to catch up"
        )
        log.error("[watch] %s", msg)
        if notifier is not None:
            await notifier.notify(msg, severity=Severity.ERROR)
        return None

    async def _on_message(event: Any) -> None:
        message = event.message
        if not _has_file(message):
            return  # we catalog only files
        try:
            async with session_factory() as session:
                fs = FileSystem(
                    NodeRepo(session),
                    master_channel=master,
                    gateway=user_gateway,
                    min_size=config.telegram.upload.min_size,
                )
                node = await fs.import_message(master, message.id, parent_id=ROOT_ID)
            if node is not None:
                log.info("[watch] cataloged %s (msg %s)", node.name, message.id)
        except Exception:  # noqa: BLE001 - one bad message never kills the watcher
            log.exception("[watch] failed to catalog msg %s", message.id)

    client.add_event_handler(_on_message, events.NewMessage(chats=master))
    log.info("[watch] main_bot listening on master channel %s", master)
    return client
