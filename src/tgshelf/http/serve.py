"""`serve` composition root: build the runtime from config and run the HTTP app.

The pure wiring (account classification, rate limiter, engine assembly) is unit
tested. Starting the real Telegram clients and running the server are the
Telegram/IO boundary, exercised by manual smoke tests.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any, Sequence

from aiohttp import web

from tgshelf.config import Config, RateLimitConfig
from tgshelf.constants import PART_SIZE
from tgshelf.core.executor import FsExecutor
from tgshelf.core.uploader import Uploader
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.http.app import make_app
from tgshelf.looplag import start_loop_lag_monitor, stop_loop_lag_monitor
from tgshelf.telegram.pool import BotPool, ClientPool, PoolMember
from tgshelf.telegram.ratelimit import InMemoryRateLimiter

log = logging.getLogger("tgshelf.serve")


class ServeError(Exception):
    """The runtime cannot be assembled (e.g. no usable account)."""


# -- pure composition (unit tested) ----------------------------------------


def build_pools(
    clients: Sequence[tuple[Any, Any]], *, capacity: int = 4
) -> tuple[ClientPool, BotPool]:
    """Split (account_config, started_client) pairs into the user pool (upload /
    management) and the bot pool (download / streaming)."""
    users: list[PoolMember] = []
    bots: list[PoolMember] = []
    for account, client in clients:
        member = PoolMember(client=client, name=account.name, capacity=capacity)
        (bots if account.is_bot else users).append(member)
    if not users:
        raise ServeError("at least one user account is required (upload/management)")
    return ClientPool(users), BotPool(bots)


def make_rate_limiter(rate_limit: RateLimitConfig):
    if rate_limit.calls <= 0:
        return None
    if rate_limit.coordination == "redis":
        raise NotImplementedError(
            "redis rate-limit coordination is not implemented yet (memory only)"
        )
    return InMemoryRateLimiter(max_calls=rate_limit.calls, window=rate_limit.window)


def build_runtime(config: Config, session_factory, clients) -> dict[str, Any]:
    """Assemble pools + engines + executor from started clients. Returns the
    components the HTTP routes will use."""
    client_pool, bot_pool = build_pools(clients)
    # with N>1 connections per client, the engine must fire >= N concurrent saves
    # to keep them busy; the client round-robins them across the connections.
    uploader = Uploader(
        client_pool, part_size=PART_SIZE,
        max_in_flight=max(3, config.concurrent_tcp_connections),
    )
    streamer_pool = bot_pool if bot_pool.members else client_pool
    from tgshelf.core.download import ParallelStreamer

    streamer = ParallelStreamer(
        streamer_pool,
        k=config.download.multi_bot_download,
        chunk_timeout=config.download.chunk_timeout,
        user_pool=client_pool,
        allow_user_fallback=config.download.allow_user_fallback,
        memory_soft_limit=config.download.memory_soft_limit,
    )
    executor = FsExecutor(
        session_factory,
        client_pool,
        master_channel=config.telegram.upload.channel,
        concurrent=config.operations.concurrent,
        min_size=config.telegram.upload.min_size,
        uploader=uploader,
        streamer=streamer,
    )
    return {
        "config": config,
        "session_factory": session_factory,
        "client_pool": client_pool,
        "bot_pool": bot_pool,
        "uploader": uploader,
        "streamer": streamer,
        "executor": executor,
    }


# -- Telegram / IO boundary (smoke tested) ---------------------------------


async def start_clients(config: Config, rate_limiter) -> list[tuple[Any, Any]]:
    """Connect each configured account from its stored session. Accounts without
    a usable session are skipped with a warning (run `accounts login`). The
    `main_bot` watcher is NOT here — it is a dedicated instance started separately
    by serve (its own token, receive_updates=True), never a pool client."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from tgshelf.telegram.client import TgClient
    from tgshelf.telegram.session_store import build_session_store

    store_session = None
    engine = None
    if config.session_storage == "db":
        engine = create_engine(config.db)
        store_session = await create_session_factory(engine)().__aenter__()
    store = build_session_store(
        config.session_storage, data_dir=Path(config.data), session=store_session
    )

    clients: list[tuple[Any, Any]] = []
    try:
        for account in config.telegram.users:
            session_str = await store.load(account.name)
            if not session_str:
                log.warning(
                    "account '%s' has no session; run `tgshelf accounts login %s`",
                    account.name, account.name,
                )
                continue
            tele = TelegramClient(
                StringSession(session_str), account.api_id, account.api_hash,
                receive_updates=False,
                # don't let Telethon silently sleep on FloodWaits behind our back
                # (default 60s): surface them to our middleware so they are logged
                # ([flood]) and the streamer can fail over to another bot instead
                # of stalling this connection ~1s mid-stream.
                flood_sleep_threshold=0,
            )
            await tele.connect()
            if not await tele.is_user_authorized():
                log.warning("session for '%s' is not authorized; re-login", account.name)
                await tele.disconnect()
                continue
            # Bots are leased only by the streamer (which fails over on cooldown).
            # Telegram's "natural" sub-second FloodWaits are absorbed inline (DEBUG,
            # no warning, no bot swap) — the read-ahead buffer covers them; only a
            # FloodWait >= chunk_timeout surfaces as FloodCooldown -> [flood] WARNING
            # + failover. Threshold is chunk_timeout-1 so the inline sleep never
            # races the per-chunk wait_for(chunk_timeout). User accounts (upload, no
            # failover) keep the default inline-sleep threshold.
            tg_kwargs = (
                {"flood_threshold": max(0, int(config.download.chunk_timeout) - 1)}
                if account.is_bot else {}
            )
            clients.append((account, TgClient(
                tele, name=account.name, rate_limiter=rate_limiter,
                tcp_connections=config.concurrent_tcp_connections, **tg_kwargs,
            )))
    finally:
        if store_session is not None:
            await store_session.close()
        if engine is not None:
            await engine.dispose()
    return clients


async def run_server(config: Config) -> None:
    if not config.http.enabled:
        raise ServeError("http.enabled is false; nothing to serve")

    engine = create_engine(config.db)
    session_factory = create_session_factory(engine)
    rate_limiter = make_rate_limiter(config.telegram.rate_limit)

    clients = await start_clients(config, rate_limiter)
    runtime = build_runtime(config, session_factory, clients)

    from tgshelf.core.notify import Notifier

    # the alert channel writer: a user client pushes grave conditions (watcher
    # failures, critical move-cleanup failures) to telegram.notify.channel. Built
    # once here and shared by the executor (folder moves), per-request FS (file
    # moves) and the watcher. (Which account writes is still open — see PLAN.md.)
    user_gateway = (
        runtime["client_pool"].members[0].client
        if runtime["client_pool"].members
        else None
    )
    notifier = Notifier(
        client=getattr(user_gateway, "_client", None),
        channel=config.telegram.notify.channel,
    )
    runtime["executor"]._notifier = notifier

    from tgshelf.http.api import register_routes
    from tgshelf.http.download import register_download_routes
    from tgshelf.http.ops import register_ops_routes
    from tgshelf.http.upload import register_upload_routes
    from tgshelf.http.webui import register_webui_routes

    app = make_app(
        config.http,
        session_factory=session_factory,
        master_channel=config.telegram.upload.channel,
        min_size=config.telegram.upload.min_size,
        executor=runtime["executor"],
        uploader=runtime["uploader"],
        streamer=runtime["streamer"],
        client_pool=runtime["client_pool"],
        bot_pool=runtime["bot_pool"],
        notifier=notifier,
        strm=config.strm,
    )
    register_routes(app)  # JSON metadata + tree (B2)
    register_download_routes(app)  # streaming download (B3)
    register_upload_routes(app)  # streaming upload (B3)
    register_ops_routes(app)  # /status (B3)
    register_webui_routes(app)  # React SPA at / — LAST (catch-all fallback)

    # start the live channel watcher (no-op if not configured); fetches posted
    # documents through a user client from the pool. The watcher is best-effort:
    # any failure is logged + pushed to the notify channel, never fatal to serve.
    from tgshelf.bot.watcher import start_watcher

    watcher_client = await start_watcher(
        config, session_factory=session_factory, user_gateway=user_gateway, notifier=notifier
    )

    # handler_cancellation: cancel the request task as soon as the client
    # disconnects. A streaming player that seeks closes the old connection while
    # our handler may be parked in the streamer's ordered-reassembly wait (not
    # writing, so a disconnect would otherwise go unnoticed and leak its leased
    # bots). Cancellation reaches the handler -> finally aclose() -> bots freed
    # immediately, so the new seek stream is never starved.
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    for host in (h.strip() for h in config.http.host.split(",") if h.strip()):
        # shutdown_timeout is the GRACE aiohttp waits for in-flight handlers to
        # finish on their own before cancelling them. A streaming response never
        # finishes (whole-file range), so any grace is just wasted "flush" time on
        # Ctrl-C. Keep it tiny (NOT 0 — ceil_timeout treats <=0 as "no timeout" =
        # wait forever): ~0.1s grace, then handler_cancellation cancels the streams
        # (finally -> aclose -> bots freed) almost immediately.
        await web.TCPSite(runner, host, config.http.port, shutdown_timeout=0.1).start()
        log.info("serving on %s:%s", host, config.http.port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    # watch the watcher: if the bot drops while serving, log + notify and keep
    # serving (no live cataloging until restart; `import-channel` backfills)
    monitor = (
        asyncio.create_task(_watch_health(watcher_client, notifier, stop))
        if watcher_client is not None
        else None
    )
    # at debug, flag event-loop stalls that would freeze every in-flight stream at once
    lag_task = start_loop_lag_monitor() if log.isEnabledFor(logging.DEBUG) else None
    try:
        await stop.wait()
    finally:
        await stop_loop_lag_monitor(lag_task)
        if monitor is not None:
            monitor.cancel()
        await runner.cleanup()
        if watcher_client is not None:
            await watcher_client.disconnect()
        for _account, client in clients:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()  # disconnect the per-DC media senders first
            disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
            if disconnect is not None:
                await disconnect()
        await engine.dispose()
        log.info("stopped")


async def _watch_health(watcher_client, notifier, stop: asyncio.Event) -> None:
    """Notify (never raise) if the watcher bot disconnects on its own while serve
    is still up. A normal shutdown sets `stop` first, so it is not reported."""
    try:
        await watcher_client.disconnected
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - monitoring must never crash serve
        pass
    if stop.is_set():
        return
    msg = (
        "main_bot watcher disconnected; serving continues WITHOUT "
        "live cataloging — run `tgshelf import-channel` to catch up"
    )
    log.error("[watch] %s", msg)
    if notifier is not None:
        from tgshelf.telegram.errors import Severity

        await notifier.notify(msg, severity=Severity.ERROR)
