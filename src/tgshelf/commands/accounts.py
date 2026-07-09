"""`tgshelf accounts` — manage Telegram accounts and their sessions.

Subcommands: list, setup, login (interactive user), add-bot (bot token from
config), import (legacy .session file). Sessions are persisted via the backend
selected by `session_storage` (db | file).
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
        "setup": _setup,
        "login": _login,
        "add-bot": _add_bot,
        "check": _check,
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


@dataclass(frozen=True)
class AccountCheckResult:
    name: str
    ok: bool
    premium: bool | None = None
    max_upload_parts: int | None = None
    dc_id: int | None = None
    user_id: int | None = None
    error: str | None = None


def _selected_user_accounts(config: Config, names: list[str]) -> tuple[list[AccountConfig], int]:
    if not names:
        return [account for account in config.telegram.users if not account.is_bot], 0
    selected: list[AccountConfig] = []
    for name in names:
        account = _account(config, name)
        if account is None:
            print(f"error: account '{name}' not found in config", file=sys.stderr)
            return [], 1
        if account.is_bot:
            print(
                f"error: '{name}' is a bot; accounts check validates user accounts only",
                file=sys.stderr,
            )
            return [], 1
        selected.append(account)
    return selected, 0


async def _check_one_user(account: AccountConfig, store) -> AccountCheckResult:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    session_str = await store.load(account.name)
    if not session_str:
        return AccountCheckResult(account.name, ok=False, error="missing session")

    client = TelegramClient(
        StringSession(session_str),
        account.api_id,
        account.api_hash,
        receive_updates=False,
        flood_sleep_threshold=0,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return AccountCheckResult(account.name, ok=False, error="session not authorized")
        caps = await auth.fetch_caps(client)
        await store.save(
            account.name,
            client.session.save(),
            kind="user",
            api_id=account.api_id,
            api_hash=account.api_hash,
            dc_id=caps.dc_id,
            is_premium=caps.is_premium,
        )
        return AccountCheckResult(
            account.name,
            ok=True,
            premium=caps.is_premium,
            max_upload_parts=caps.max_upload_parts,
            dc_id=caps.dc_id,
            user_id=caps.user_id,
        )
    finally:
        await client.disconnect()


def _print_check_result(result: AccountCheckResult) -> None:
    if result.ok:
        print(
            f"{result.name}: OK "
            f"Premium: {result.premium} "
            f"Max parts: {result.max_upload_parts} "
            f"DC: {result.dc_id} "
            f"User ID: {result.user_id}"
        )
        return
    print(f"{result.name}: ERROR {result.error}")


async def _check(config: Config, args) -> int:
    selected, rc = _selected_user_accounts(config, list(getattr(args, "names", [])))
    if rc:
        return rc
    if not selected:
        print("error: no user accounts configured", file=sys.stderr)
        return 1
    failed = False
    async with _open_store(config) as store:
        for account in selected:
            try:
                result = await _check_one_user(account, store)
            except Exception as exc:  # noqa: BLE001 - report per-account diagnostics
                result = AccountCheckResult(account.name, ok=False, error=str(exc))
            _print_check_result(result)
            failed = failed or not result.ok
    return 1 if failed else 0


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


def _configured_bots(config: Config) -> list[AccountConfig]:
    return [account for account in config.telegram.users if account.is_bot and account.bot_token]


def _bot_accounts(config: Config, args) -> tuple[list[AccountConfig], int]:
    if getattr(args, "all", False):
        if getattr(args, "names", []):
            print("error: use either bot names or --all, not both", file=sys.stderr)
            return [], 1
        bots = _configured_bots(config)
        if not bots:
            print("error: no bots configured with bot_token", file=sys.stderr)
            return [], 1
        return bots, 0

    names = getattr(args, "names", None)
    if names is None and hasattr(args, "name"):
        names = [args.name]
    if not names:
        print("error: provide at least one bot name or --all", file=sys.stderr)
        return [], 1

    selected: list[AccountConfig] = []
    for name in names:
        account = _account(config, name)
        if account is None:
            print(f"error: account '{name}' not found in config", file=sys.stderr)
            return [], 1
        if not account.is_bot or not account.bot_token:
            print(f"error: '{name}' has no bot_token in config", file=sys.stderr)
            return [], 1
        selected.append(account)
    return selected, 0


async def _register_bot(account: AccountConfig, store) -> None:
    assert account.bot_token is not None
    await auth.login_bot(
        account.name, account.api_id, account.api_hash, account.bot_token, store
    )


async def _add_bot(config: Config, args) -> int:
    bot_accounts, rc = _bot_accounts(config, args)
    if rc:
        return rc
    async with _open_store(config) as store:
        for account in bot_accounts:
            await _register_bot(account, store)
            print(f"registered bot '{account.name}'")
    return 0


async def _setup(config: Config, args) -> int:
    force = bool(getattr(args, "force", False))
    user_logins = 0
    bot_registrations = 0
    skipped = 0
    async with _open_store(config) as store:
        for account in config.telegram.users:
            if not force and await store.load(account.name):
                kind = "bot" if account.is_bot else "user"
                print(f"skipped '{account.name}' ({kind}, session already exists)")
                skipped += 1
                continue
            if account.is_bot:
                if not account.bot_token:
                    print(f"error: '{account.name}' has no bot_token in config", file=sys.stderr)
                    return 1
                await _register_bot(account, store)
                print(f"registered bot '{account.name}'")
                bot_registrations += 1
                continue
            caps = await auth.login_user(
                account.name, account.api_id, account.api_hash, store,
                code_callback=lambda: input("Telegram code: "),
            )
            tier = "premium" if caps.is_premium else "free"
            print(
                f"logged in '{account.name}' (user, {tier}, max parts {caps.max_upload_parts})"
            )
            user_logins += 1
    print(
        "setup complete: "
        f"{user_logins} user login{'' if user_logins == 1 else 's'}, "
        f"{bot_registrations} bot registration{'' if bot_registrations == 1 else 's'}, "
        f"{skipped} skipped"
    )
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
