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

LEVELS = {
    "no": logging.CRITICAL,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

NOISY_LOGGERS = ("telethon", "aiohttp.access", "sqlalchemy.engine", "alembic")

FORMAT = "[%(asctime)s][%(name)s][%(levelname)s] %(message)s"
DATE_FORMAT = "%d/%m/%Y %H:%M:%S"


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
    # aiohttp logs client disconnects on its own server logger, outside our
    # middleware; drop just those records (keep genuine handler errors).
    server_log = logging.getLogger("aiohttp.server")
    server_log.filters = [f for f in server_log.filters if not isinstance(f, _DropConnectionErrors)]
    server_log.addFilter(_DropConnectionErrors())
