"""Logging setup.

Maps the config `logger` level names to stdlib levels and quiets the noisy
third-party loggers (telethon logs every update, aiohttp.access every streamed
request, sqlalchemy.engine every statement) which would drown application logs
even at INFO.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from contextvars import ContextVar

LEVELS = {
    "no": logging.CRITICAL,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

NOISY_LOGGERS = ("telethon", "aiohttp.access", "sqlalchemy.engine", "alembic")

# Exceptions that mean "the peer we are writing to went away", never a server
# fault: a reset/aborted/broken socket, or the cooperative cancellation aiohttp
# raises (handler_cancellation) when the client disconnects mid-stream. These
# are logged quietly (no stacktrace) wherever they surface.
#
# Deliberately EXCLUDES the broad `ConnectionError`: `ConnectionRefusedError`,
# DNS `gaierror`, `TimeoutError` etc. are OUTBOUND connect failures (the DB or an
# upstream is down/misconfigured) and MUST surface as real errors — they used to
# be swallowed here, hiding a dead-DB 500 with no log at all.
CLIENT_DISCONNECT = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    asyncio.CancelledError,
)


def describe_exc(exc: BaseException) -> str:
    """A compact one-line ``Type: message`` chain following ``__cause__`` /
    ``__context__``. A SQLAlchemy error wrapping an asyncpg/OS error shows the
    underlying driver cause too, so the real reason ('Connection refused', name
    resolution, …) is visible without expanding the stacktrace."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        message = str(cur).strip()
        parts.append(f"{type(cur).__name__}: {message}" if message else type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
    return "  <-  ".join(parts)

# request/stream correlation id: set once per HTTP stream or per CLI download
# file, it tags EVERY log line emitted while serving it (handler + streamer
# workers, which inherit the context via asyncio.create_task). Grep one id to
# reconstruct a whole request: which bots were leased and how each fetch went.
_request_id: ContextVar[str] = ContextVar("tgshelf_request_id", default="-")

FORMAT = "[%(asctime)s][%(name)s][%(levelname)s][%(request_id)s] %(message)s"
DATE_FORMAT = "%d/%m/%Y %H:%M:%S"


def new_request_id() -> str:
    """Mint a short id and bind it to the current context; returns it for the
    caller to log at request arrival."""
    rid = uuid.uuid4().hex[:8]
    _request_id.set(rid)
    return rid


def current_request_id() -> str:
    return _request_id.get()


class _RequestIdFilter(logging.Filter):
    """Stamp every record with the current request id (default '-')."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


class _DropConnectionErrors(logging.Filter):
    """Drop records whose exception is a client-disconnect (socket reset /
    aborted / cancellation). aiohttp's server logger reports these as "Error
    handling request" with a stacktrace when a streaming client goes away mid
    response — noise, not a server fault. Real errors (incl. an unreachable DB,
    which is a ConnectionRefusedError, NOT a client disconnect) keep their
    stacktrace — see CLIENT_DISCONNECT."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, CLIENT_DISCONNECT)


# telethon WARNING prefixes that are benign chatter, not faults: the server
# routinely drops idle/extra MTProto connections and telethon just reconnects.
# Far more frequent now that each client opens several connections per DC
# (concurrent_tcp_connections). Matched against the formatted message; only the
# listed prefixes are dropped, every other telethon warning still gets through.
_BENIGN_TELETHON_PREFIXES = ("Server closed the connection",)


class _DropBenignTelethon(logging.Filter):
    """Drop the handful of known-benign telethon connection WARNINGs (see
    `_BENIGN_TELETHON_PREFIXES`). Attached to the root handler so it also sees
    records propagated from telethon's sub-loggers (telethon.network.*)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("telethon") and record.levelno == logging.WARNING:
            message = record.getMessage()
            if any(message.startswith(p) for p in _BENIGN_TELETHON_PREFIXES):
                return False
        return True


def setup_logging(level_name: str) -> None:
    logging.basicConfig(
        level=LEVELS[level_name],
        format=FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
        force=True,  # idempotent: replaces handlers on reconfiguration
    )
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # stamp the request id on every emitted record (on the handler so it covers
    # records propagated from child loggers too) — needed by the FORMAT above.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _RequestIdFilter) for f in handler.filters):
            handler.addFilter(_RequestIdFilter())
        # drop benign telethon reconnection chatter (covers propagated records
        # from telethon.network.* since it sits on the root handler).
        if not any(isinstance(f, _DropBenignTelethon) for f in handler.filters):
            handler.addFilter(_DropBenignTelethon())
    # aiohttp logs client disconnects on its own server logger, outside our
    # middleware; drop just those records (keep genuine handler errors).
    server_log = logging.getLogger("aiohttp.server")
    server_log.filters = [f for f in server_log.filters if not isinstance(f, _DropConnectionErrors)]
    server_log.addFilter(_DropConnectionErrors())
