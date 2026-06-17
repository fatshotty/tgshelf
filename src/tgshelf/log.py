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
    """Drop records whose exception is a client-disconnect (ConnectionError /
    reset / cancellation). aiohttp's server logger reports these as "Error
    handling request" with a stacktrace when a streaming client goes away mid
    response — noise, not a server fault. Real errors keep their stacktrace."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, (ConnectionError, asyncio.CancelledError))


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
    # aiohttp logs client disconnects on its own server logger, outside our
    # middleware; drop just those records (keep genuine handler errors).
    server_log = logging.getLogger("aiohttp.server")
    server_log.filters = [f for f in server_log.filters if not isinstance(f, _DropConnectionErrors)]
    server_log.addFilter(_DropConnectionErrors())
