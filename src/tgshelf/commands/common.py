"""Shared command-line resolution helpers."""

from __future__ import annotations

import os
from typing import Mapping

from tgshelf.config import Config


def resolve_concurrent(
    config: Config,
    *,
    cli_value: int | None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve operation concurrency with stable precedence.

    Precedence is always:
      1. CONCURRENCY environment variable
      2. CLI --concurrent value
      3. config.operations.concurrent
    """
    if env is None:
        env = os.environ
    raw_env = env.get("CONCURRENCY")
    if raw_env not in (None, ""):
        try:
            value = int(raw_env)
        except ValueError:
            raise ValueError("CONCURRENCY must be an integer >= 1") from None
    elif cli_value is not None:
        value = int(cli_value)
    else:
        value = int(config.operations.concurrent)
    if value < 1:
        raise ValueError("concurrency must be >= 1")
    return value
