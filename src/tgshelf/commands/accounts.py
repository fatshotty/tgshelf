"""`tgshelf accounts` — manage Telegram accounts and their sessions.

Subcommands: list, login (interactive user), add-bot (bot token from config),
import (legacy .session file). Sessions are persisted via the backend selected
by `session_storage` (db | file).
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from tgshelf.config import AccountConfig, Config
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.telegram import auth
from tgshelf.telegram.session_store import build_session_store


@asynccontextmanager
async def _open_store(config: Config):
    """Yield a SessionStore, managing the DB engine/session when needed."""
    if config.session_storage == "file":
        yield build_session_store("file", data_dir=Path(config.data), session=None)
        return
    engine = create_engine(config.db)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            yield build_session_store("db", data_dir=Path(config.data), session=session)
    finally:
        await engine.dispose()


def _account(config: Config, name: str) -> AccountConfig | None:
    return next((u for u in config.telegram.users if u.name == name), None)


async def run(config: Config, args) -> int:
    handler = {
        "list": _list,
        "login": _login,
        "add-bot": _add_bot,
        "import": _import,
    }.get(args.accounts_cmd)
    if handler is None:
        print(f"error: unknown accounts subcommand {args.accounts_cmd!r}", file=sys.stderr)
        return 2
    return await handler(config, args)


async def _list(config: Config, args=None) -> int:
    async with _open_store(config) as store:
        print(f"{'NAME':<20} {'KIND':<6} {'SESSION':<8}")
        for user in config.telegram.users:
            kind = "bot" if user.is_bot else "user"
            has_session = "yes" if await store.load(user.name) else "no"
            print(f"{user.name:<20} {kind:<6} {has_session:<8}")
    return 0


async def _login(config: Config, args) -> int:
    account = _account(config, args.name)
    if account is None:
        print(f"error: account '{args.name}' not found in config", file=sys.stderr)
        return 1
    if account.is_bot:
        print(
            f"error: '{args.name}' is a bot; use `accounts add-bot`", file=sys.stderr
        )
        return 1
    async with _open_store(config) as store:
        caps = await auth.login_user(
            account.name, account.api_id, account.api_hash, store,
            code_callback=lambda: input("Telegram code: "),
        )
    tier = "premium" if caps.is_premium else "free"
    print(f"logged in '{account.name}' (user, {tier}, max parts {caps.max_upload_parts})")
    return 0


async def _add_bot(config: Config, args) -> int:
    account = _account(config, args.name)
    if account is None:
        print(f"error: account '{args.name}' not found in config", file=sys.stderr)
        return 1
    if not account.is_bot or not account.bot_token:
        print(f"error: '{args.name}' has no bot_token in config", file=sys.stderr)
        return 1
    async with _open_store(config) as store:
        await auth.login_bot(
            account.name, account.api_id, account.api_hash, account.bot_token, store
        )
    print(f"registered bot '{account.name}'")
    return 0


async def _import(config: Config, args) -> int:
    account = _account(config, args.name)
    if account is None:
        print(f"error: account '{args.name}' not found in config", file=sys.stderr)
        return 1
    try:
        session_string = auth.session_string_from_sqlite(Path(args.session))
    except FileNotFoundError:
        print(f"error: session file not found: {args.session}", file=sys.stderr)
        return 1
    async with _open_store(config) as store:
        await store.save(
            account.name,
            session_string,
            kind="bot" if account.is_bot else "user",
            api_id=account.api_id,
            api_hash=account.api_hash,
            bot_token=account.bot_token,
        )
    print(f"imported session for '{account.name}'")
    return 0
