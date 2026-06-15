"""`tgshelf create-bots` and `tgshelf bots check`.

Two admin commands sharing the same building blocks (port of the legacy
`python/commands/create_bots.py`):

- **create-bots**: drives a BotFather conversation through a user account to
  create N bots, then promotes each to read-only admin of selected channels
  (promoting = adding). Interactive (account + channel selection), like legacy.
- **bots check**: for every configured bot and every channel in use, verifies
  membership via the user account and repairs (re-promotes) what is missing.

There is no request/response API with BotFather: we send a message and poll the
chat history for a newer reply (`send_and_wait`). The pure helpers below (naming,
token extraction, reply classification) are unit-tested; the conversation and the
promotion touch real Telegram and are covered by manual smoke.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Sequence

from telethon import TelegramClient, utils
from telethon import errors as tg_errors
from telethon.sessions import StringSession
from telethon.tl.functions.channels import EditAdminRequest
from telethon.tl.types import ChatAdminRights

from tgshelf.commands.accounts import _open_store
from tgshelf.config import AccountConfig, Config
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.db.repo import NodeRepo

log = logging.getLogger("tgshelf.bots")

BOTFATHER = "BotFather"

# BotFather token: "<8-12 digits>:<35+ url-safe chars>" (legacy create_bots.py:166)
_TOKEN_RE = re.compile(r"(\d{8,12}:[A-Za-z0-9_-]{35,})")

# classify_botfather_reply outcomes
REPLY_OK = "ok"
REPLY_BUSY = "busy"  # a /newbot dialog is already open -> /cancel and retry
REPLY_REJECTED = "rejected"  # the chosen username was refused


def bot_username(prefix: str, n: int) -> str:
    """Deterministic bot username: `{prefix}_{NN}_bot`, zero-padded to 2 digits
    (legacy naming, e.g. redstream_07_bot)."""
    return f"{prefix}_{n:02d}_bot"


def extract_bot_token(text: str | None) -> str | None:
    """Pull a bot token out of a BotFather reply, or None if absent."""
    match = _TOKEN_RE.search(text or "")
    return match.group(1) if match else None


def classify_botfather_reply(text: str | None) -> str:
    """Read a BotFather reply: REPLY_BUSY when an earlier /newbot is still open
    ('already have'/'use /cancel'), REPLY_REJECTED when the username was refused
    ('sorry'/'already taken'/'invalid'), otherwise REPLY_OK."""
    low = (text or "").lower()
    if "already have" in low or "use /cancel" in low:
        return REPLY_BUSY
    if "sorry" in low or "already taken" in low or "invalid" in low:
        return REPLY_REJECTED
    return REPLY_OK


def parse_channel_selection(text: str | None, available: Sequence[int]) -> list[int]:
    """Interpret a channel selection against an ordered list of channel ids:
    empty -> none, 'all' -> all, otherwise comma-separated 1-based indexes
    (out-of-range/garbage ignored). Duplicates collapse, order preserved."""
    text = (text or "").strip()
    if not text:
        return []
    if text.lower() == "all":
        return list(available)
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(available) and available[idx] not in out:
                out.append(available[idx])
    return out


class BotCommandError(Exception):
    """A create-bots / bots-check failure that aborts the command."""


# -- BotFather conversation (FSM testable; the I/O is injected) -------------

Say = Callable[..., Awaitable[str]]


async def create_bot_via_botfather(say: Say, username: str, display_name: str) -> str:
    """Drive the /newbot dialog and return the bot token.

    `say(text, timeout=...)` sends one message to BotFather and returns its next
    reply (real I/O in the command, scripted in tests). Recovers a dirty dialog
    (a still-open /newbot) with /cancel, raises BotCommandError when the username
    is refused or no token comes back. Steps mirror legacy create_bots.py."""
    reply = await say("/newbot")
    if classify_botfather_reply(reply) == REPLY_BUSY:
        await say("/cancel", timeout=10)
        reply = await say("/newbot")

    await say(display_name)  # BotFather now asks for the username
    reply = await say(username, timeout=20)  # the slowest step

    if classify_botfather_reply(reply) == REPLY_REJECTED:
        raise BotCommandError(f"BotFather rejected username @{username}: {reply}")
    token = extract_bot_token(reply)
    if token is None:
        raise BotCommandError(f"could not extract token from BotFather reply:\n{reply}")
    return token


async def send_and_wait(
    client: TelegramClient,
    text: str,
    *,
    timeout: float = 15.0,
    poll: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Send `text` to BotFather and poll the chat for its reply (there is no
    request/response API). Remembers the last message id, sends (sleeping through
    a FloodWait if BotFather rate-limits us), then polls every `poll`s for a newer
    INCOMING message until `timeout`."""
    history = await client.get_messages(BOTFATHER, limit=1)
    last_id = history[0].id if history else 0

    while True:
        try:
            await client.send_message(BOTFATHER, text)
            break
        except tg_errors.FloodWaitError as exc:
            log.warning("[flood] BotFather FloodWait %ds; waiting", exc.seconds)
            await sleep(exc.seconds + 1)

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await sleep(poll)
        msgs = await client.get_messages(BOTFATHER, limit=1)
        if msgs and msgs[0].id > last_id and not getattr(msgs[0], "out", False):
            return msgs[0].message or ""
    raise BotCommandError(f"timeout waiting for BotFather reply after: {text!r}")


# Read-only admin = ONLY "manage chat" (ChatAdminRights.other); every concrete
# permission stays False. NB: Telethon's high-level edit_admin defaults every
# unset permission to is_admin, so edit_admin(is_admin=True) would grant FULL
# admin — we must build the rights explicitly via the raw request. Promoting also
# ADDS the bot to the channel. Legacy promotes TWICE (Telegram may apply default
# bot rights on the first call; the second overwrites them) — second pass failing
# is only a warning.
_READONLY_ADMIN = ChatAdminRights(other=True)


async def promote_bot(
    client: TelegramClient,
    channel_id: int,
    bot: str | int,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    channel = utils.get_input_channel(await client.get_input_entity(channel_id))
    user = utils.get_input_user(await client.get_input_entity(bot))

    async def _promote() -> None:
        await client(
            EditAdminRequest(channel=channel, user_id=user, admin_rights=_READONLY_ADMIN, rank="")
        )

    await _promote()
    try:
        await sleep(2)
        await _promote()
    except Exception as exc:  # noqa: BLE001 - second pass is best-effort
        log.warning("could not re-apply @%s rights in %s: %s", bot, channel_id, exc)


# -- shared wiring: sessions, connected client, channel set -----------------

async def _load_session(config: Config, name: str) -> str | None:
    async with _open_store(config) as store:
        return await store.load(name)


@asynccontextmanager
async def _connect_user(config: Config, account: AccountConfig):
    """Yield a connected, authorized Telethon user client for `account` (its
    stored StringSession). Not a pool client: this drives BotFather and admin
    edits with Telethon's high-level helpers."""
    session = await _load_session(config, account.name)
    if not session:
        raise BotCommandError(
            f"no session for '{account.name}'; run `tgshelf accounts login {account.name}`"
        )
    client = TelegramClient(
        StringSession(session), account.api_id, account.api_hash, receive_updates=False
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise BotCommandError(
                f"session for '{account.name}' is not authorized; re-login"
            )
        yield client
    finally:
        await client.disconnect()


async def channels_in_use(config: Config) -> list[int]:
    """Ordered list of every channel a bot may need to reach: the master channel
    first, then the distinct per-folder/per-part channels from the DB (sorted)."""
    master = config.telegram.upload.channel
    engine = create_engine(config.db)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            used = await NodeRepo(session).distinct_channels()
    finally:
        await engine.dispose()
    return [master] + sorted(c for c in used if c != master)


# -- interactive selection --------------------------------------------------

def _channel_label(config: Config, channel_id: int) -> str:
    if channel_id == config.telegram.upload.channel:
        return f"{channel_id} (master)"
    return str(channel_id)


def _select_user_account(config: Config, prompt=input) -> AccountConfig:
    users = [u for u in config.telegram.users if not u.is_bot]
    if not users:
        raise BotCommandError("no user accounts in config (bots are created via a user)")
    if len(users) == 1:
        return users[0]
    print("\nAvailable user accounts:")
    for i, user in enumerate(users, 1):
        print(f"  [{i}] {user.name}")
    while True:
        choice = prompt("Account number to create bots with > ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(users):
            return users[int(choice) - 1]
        print(f"  invalid choice, enter a number between 1 and {len(users)}")


def _select_channels(config: Config, channels: list[int], prompt=input) -> list[int]:
    if not channels:
        return []
    print("\nAvailable channels:")
    for i, channel_id in enumerate(channels, 1):
        print(f"  [{i}] {_channel_label(config, channel_id)}")
    selection = prompt(
        "Channels to add the bots to (comma-separated, 'all', or ENTER to skip) > "
    )
    return parse_channel_selection(selection, channels)


def _print_config_entry(account: AccountConfig, bot_name: str, token: str) -> None:
    print("\n" + "=" * 64)
    print(f"  @{bot_name} created — add this entry to config.yaml (telegram.users):")
    print("=" * 64)
    print(f"    - name: '{bot_name}'")
    print(f"      api_id: {account.api_id}")
    print(f"      api_hash: '{account.api_hash}'")
    print(f"      bot_token: '{token}'")
    print("=" * 64)
    print(f"  then: tgshelf accounts add-bot {bot_name}\n")


# -- command entry point ----------------------------------------------------

async def run_create(config: Config, args) -> int:
    prefix = getattr(args, "prefix", None)
    count = getattr(args, "count", None)
    start = getattr(args, "start", None) or 1
    delay = getattr(args, "delay", None)
    delay = 5 if delay is None else delay
    if not prefix:
        print("error: --prefix is required (e.g. 'redstream')", file=sys.stderr)
        return 1
    if not count or count < 1:
        print("error: --count is required and must be >= 1", file=sys.stderr)
        return 1

    try:
        account = _select_user_account(config)
        channels = await channels_in_use(config)
        selected = _select_channels(config, channels)
        if not selected:
            log.warning("no channels selected — bots will be created but not joined")
    except BotCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    failures = 0
    async with _connect_user(config, account) as client:
        say = lambda text, timeout=15.0: send_and_wait(client, text, timeout=timeout)  # noqa: E731
        for i in range(count):
            n = start + i
            username = bot_username(prefix, n)
            log.info("[%d/%d] creating @%s via '%s'", i + 1, count, username, account.name)
            try:
                token = await create_bot_via_botfather(say, username, username)
            except BotCommandError as exc:
                log.error("FAILED to create @%s: %s", username, exc)
                failures += 1
                continue
            _print_config_entry(account, username, token)

            for channel_id in selected:
                try:
                    await promote_bot(client, channel_id, f"@{username}")
                    log.info("@%s added to channel %s", username, channel_id)
                except Exception as exc:  # noqa: BLE001 - one channel never aborts the batch
                    log.error("FAILED to add @%s to %s: %s", username, channel_id, exc)

            if i < count - 1:
                log.info("waiting %ds before the next bot (BotFather rate-limits)", delay)
                await asyncio.sleep(delay)

    log.info("create-bots done (%d failed)", failures)
    return 1 if failures else 0
