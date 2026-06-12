"""Logging setup.

Maps the config `logger` level names to stdlib levels and quiets the noisy
third-party loggers (telethon logs every update, aiohttp.access every streamed
request, sqlalchemy.engine every statement) which would drown application logs
even at INFO.
"""

from __future__ import annotations

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
