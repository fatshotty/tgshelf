"""Authentication and session lifecycle.

Pure Telethon (no Pyrogram, no session conversion): an account logs in
interactively once, its StringSession is persisted via the configured backend
and reused forever. Per-account upload caps are read live at login from
help.GetAppConfig (premium vs free max fileparts), as in the legacy.

Interactive login and bot login need real Telegram (manual smoke). The pure
helpers (caps extraction, .session import) are unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.functions.help import GetAppConfigRequest

from tgshelf.telegram.session_store import SessionStore

DEFAULT_MAX_FILEPARTS_FREE = 4000
DEFAULT_MAX_FILEPARTS_PREMIUM = 8000


@dataclass(frozen=True)
class AccountCaps:
    is_bot: bool
    is_premium: bool
    max_upload_parts: int
    dc_id: int
    user_id: int


def pick_max_fileparts(app_config: dict[str, Any], *, is_premium: bool) -> int:
    """Max upload parts for the account tier (from help.GetAppConfig values)."""
    if is_premium:
        return int(
            app_config.get("upload_max_fileparts_premium", DEFAULT_MAX_FILEPARTS_PREMIUM)
        )
    return int(app_config.get("upload_max_fileparts_default", DEFAULT_MAX_FILEPARTS_FREE))


def session_string_from_sqlite(path: Path) -> str:
    """Convert a legacy Telethon SQLite .session file into a portable
    StringSession (no network); raises FileNotFoundError if absent."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    # SQLiteSession wants the path without the .session suffix
    sqlite = SQLiteSession(str(path.with_suffix("")))
    try:
        return StringSession.save(sqlite)
    finally:
        sqlite.close()


def _app_config_to_dict(app_config: Any) -> dict[str, Any]:
    """Flatten a telethon JSONObject AppConfig into {key: value}."""
    result: dict[str, Any] = {}
    for item in getattr(app_config, "value", []):
        key = getattr(item, "key", None)
        value = getattr(getattr(item, "value", None), "value", None)
        if key is not None:
            result[key] = value
    return result


async def fetch_caps(client: TelegramClient) -> AccountCaps:
    """get_me + live max-fileparts from the server (only for user accounts)."""
    me = await client.get_me()
    if me.bot:
        return AccountCaps(
            is_bot=True, is_premium=False, max_upload_parts=0, dc_id=0, user_id=me.id
        )
    raw = await client(GetAppConfigRequest(hash=0))
    config = _app_config_to_dict(getattr(raw, "config", raw))
    is_premium = bool(getattr(me, "premium", False))
    return AccountCaps(
        is_bot=False,
        is_premium=is_premium,
        max_upload_parts=pick_max_fileparts(config, is_premium=is_premium),
        dc_id=client.session.dc_id,
        user_id=me.id,
    )


async def login_user(
    name: str,
    api_id: int,
    api_hash: str,
    store: SessionStore,
    *,
    phone: str | None = None,
    code_callback=None,
    password: str | None = None,
) -> AccountCaps:
    """Interactive user login; persists the StringSession via the store."""
    existing = await store.load(name)
    client = TelegramClient(StringSession(existing), api_id, api_hash)
    # only override Telethon's interactive prompts (phone/password lambdas) when
    # a value is actually supplied; passing None would disable the prompt
    start_kwargs: dict = {}
    if phone is not None:
        start_kwargs["phone"] = phone
    if password is not None:
        start_kwargs["password"] = password
    if code_callback is not None:
        start_kwargs["code_callback"] = code_callback
    await client.start(**start_kwargs)
    try:
        caps = await fetch_caps(client)
        await store.save(
            name,
            client.session.save(),
            kind="user",
            api_id=api_id,
            api_hash=api_hash,
            dc_id=caps.dc_id,
            is_premium=caps.is_premium,
        )
        return caps
    finally:
        await client.disconnect()


async def login_bot(
    name: str, api_id: int, api_hash: str, bot_token: str, store: SessionStore
) -> AccountCaps:
    """Bot login via token; persists the StringSession via the store."""
    existing = await store.load(name)
    client = TelegramClient(StringSession(existing), api_id, api_hash)
    await client.start(bot_token=bot_token)
    try:
        me = await client.get_me()
        caps = AccountCaps(
            is_bot=True, is_premium=False, max_upload_parts=0,
            dc_id=client.session.dc_id, user_id=me.id,
        )
        await store.save(
            name,
            client.session.save(),
            kind="bot",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            dc_id=caps.dc_id,
        )
        return caps
    finally:
        await client.disconnect()
