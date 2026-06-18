"""A/B download benchmark — CURRENT path (Telethon + ParallelStreamer, k=1).

Streams a real multi-part file (channel_id + ordered message_ids) through the
exact production path — ONE bot leased from a BotPool, ParallelStreamer with
k=1, same flood_threshold / chunk_timeout / borrow-return as `serve` — to
/dev/null, and prints a RECAP comparable with `bench_download_legacy.py`.

The per-chunk evidence is the production log itself: run with `--log debug` and
the `[stream]/[fetch]/[fetch-slow]/[flood]/[failover]/[quarantine]/[wait]`
markers land in `--log-file`. `bench_compare.py` parses those + the RECAP.

No production code is modified; this is an additive harness under scripts/.

Usage:
  # use a bot already configured + logged-in in config.yaml:
  python scripts/bench_download.py --channel -100123 --messages 11,12,13 --bot bot01

  # or log a bot in fresh from its token (no stored session needed):
  python scripts/bench_download.py --channel -100123 --messages 11,12,13 \
      --api-id 123456 --api-hash 0123abc... --bot-token 123456789:AA...
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass

# make `src/` importable when run from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgshelf.config import load_config  # noqa: E402
from tgshelf.constants import CHUNK_SIZE  # noqa: E402
from tgshelf.core.download import ParallelStreamer, StreamPlan  # noqa: E402
from tgshelf.log import _RequestIdFilter, new_request_id, setup_logging  # noqa: E402
from tgshelf.telegram.pool import BotPool, PoolMember  # noqa: E402

log = logging.getLogger("tgshelf.bench")


@dataclass
class _Part:
    """Minimal stand-in for a db Part — the only fields the streamer touches."""

    channel_id: int
    message_id: int
    size: int


async def _connect_named_bot(config, name: str):
    """Connect the configured bot `name` from its stored session (reuses the exact
    serve wiring: receive_updates=False, flood_sleep_threshold=0, flood_threshold)."""
    from tgshelf.http.serve import make_rate_limiter, start_clients

    rate = make_rate_limiter(config.telegram.rate_limit)
    clients = await start_clients(config, rate)
    for account, tg in clients:
        if account.name == name:
            if not account.is_bot:
                raise SystemExit(f"account '{name}' is a USER, not a bot")
            return tg
    names = ", ".join(a.name for a, _ in clients) or "(none connected)"
    raise SystemExit(f"bot '{name}' not found / not logged in. available: {names}")


async def _connect_token(config, api_id: int, api_hash: str, token: str):
    """Log a bot in fresh from its token, wrapped exactly like serve does."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from tgshelf.telegram.client import TgClient

    tele = TelegramClient(
        StringSession(), api_id, api_hash,
        receive_updates=False, flood_sleep_threshold=0,
    )
    await tele.start(bot_token=token)  # type: ignore[arg-type]
    return TgClient(
        tele, name="bench-bot",
        flood_threshold=max(0, int(config.download.chunk_timeout) - 1),
        rate_limiter=None,
    )


async def run(args) -> None:
    config = load_config(args.config)

    if args.bot:
        tg = await _connect_named_bot(config, args.bot)
    else:
        if not (args.api_id and args.api_hash and args.bot_token):
            raise SystemExit("provide --bot <name> OR --api-id/--api-hash/--bot-token")
        tg = await _connect_token(config, args.api_id, args.api_hash, args.bot_token)

    bot_name = tg.name
    member = PoolMember(client=tg, name=bot_name, capacity=4)
    bot_pool = BotPool([member])
    streamer = ParallelStreamer(
        bot_pool,
        k=1,
        chunk_timeout=config.download.chunk_timeout,
        user_pool=None,
        allow_user_fallback=False,
        memory_soft_limit=0,
    )

    channel = args.channel
    msg_ids = [int(m) for m in args.messages.split(",") if m.strip()]

    # resolve each part's size (and DC) the same way the stream does
    log.info("[bench] resolving %d part(s) on channel %s via '%s'", len(msg_ids), channel, bot_name)
    parts: list[_Part] = []
    for mid in msg_ids:
        ref = await tg.get_document(channel, mid)
        if ref is None:
            raise SystemExit(f"message {mid} on {channel} has no downloadable document")
        parts.append(_Part(channel_id=channel, message_id=mid, size=ref.size))
        log.info("[bench] part msg=%s dc=%s size=%d", mid, getattr(ref, "dc_id", None), ref.size)

    total = sum(p.size for p in parts)
    plan = StreamPlan.build([p.size for p in parts], 0, total - 1, chunk_size=CHUNK_SIZE)

    new_request_id()  # tag the stream + its worker in the log
    log.info("[bench] START current path: bot='%s' k=1 chunk_timeout=%.1fs total=%d B (%d chunk(s))",
             bot_name, config.download.chunk_timeout, total, len(plan.chunks))

    got = 0
    t0 = time.monotonic()
    try:
        async for chunk in streamer.stream(parts, plan, channel):
            got += len(chunk)
    finally:
        dt = time.monotonic() - t0
        m = streamer.metrics()
        avg = (got / (1024 * 1024) / dt) if dt > 0 else 0.0
        recap = (
            f"RECAP path=current bytes={got} time={dt:.2f} avg={avg:.2f}MB/s "
            f"chunks={len(plan.chunks)} quarantined={member.quarantined} "
            f"consecutive_errors={member.consecutive_errors} "
            f"cooldown_remaining={max(0.0, member.cooldown_until - time.monotonic()):.1f} "
            f"degraded_total={m['degraded_total']}"
        )
        log.info("%s", recap)  # also lands in --log-file for bench_compare
        disc = getattr(getattr(tg, "_client", None), "disconnect", None)
        if disc is not None:
            await disc()


def main() -> None:
    p = argparse.ArgumentParser(description="current-path (Telethon) download benchmark")
    p.add_argument("--channel", required=True, type=int, help="channel id (e.g. -100123...)")
    p.add_argument("--messages", required=True, help="ordered message ids, comma-separated")
    p.add_argument("--bot", help="name of a configured+logged-in bot account")
    p.add_argument("--api-id", type=int, help="api id (with --bot-token instead of --bot)")
    p.add_argument("--api-hash", help="api hash (with --bot-token)")
    p.add_argument("--bot-token", help="bot token (with --api-id/--api-hash)")
    p.add_argument("--config", default="./config.yaml")
    p.add_argument("--log", default="debug", help="log level: no|error|warn|info|debug")
    p.add_argument("--log-file", help="also write the log to this file")
    args = p.parse_args()

    setup_logging(args.log)
    if args.log_file:
        fh = logging.FileHandler(args.log_file, mode="w")
        fh.setFormatter(logging.Formatter(
            "[%(asctime)s][%(name)s][%(levelname)s][%(request_id)s] %(message)s",
            datefmt="%d/%m/%Y %H:%M:%S",
        ))
        fh.addFilter(_RequestIdFilter())  # FORMAT needs request_id on every record
        logging.getLogger().addHandler(fh)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
