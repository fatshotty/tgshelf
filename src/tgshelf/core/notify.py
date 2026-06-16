"""Notifier: route grave conditions to the log AND an optional Telegram alert
channel.

Minimal seed of the full plan (severity queue, dedupe/aggregation of WARNINGs,
catalog of events are a FUTURE point — see PLAN.md): it logs by severity and
best-effort sends a line to `telegram.notify.channel` via a user client.

`notify()` NEVER raises and NEVER blocks the caller meaningfully: a broken alert
path (channel gone, account flooded) must not take down a stream, the watcher, or
an upload. The channel is optional — with no channel configured it stays log-only.
"""

from __future__ import annotations

import logging
from typing import Any

from tgshelf.telegram.errors import Severity

log = logging.getLogger("tgshelf.notify")

_LEVEL = {
    Severity.CRITICAL: logging.ERROR,
    Severity.ERROR: logging.ERROR,
    Severity.WARNING: logging.WARNING,
}


class Notifier:
    def __init__(self, *, client: Any = None, channel: int | None = None):
        # `client` is a connected Telethon client (has async send_message), used
        # only to push the alert; None or no channel -> notifications stay log-only.
        self._client = client
        self._channel = channel

    async def notify(self, message: str, *, severity: Severity = Severity.ERROR) -> None:
        """Always log; additionally send to the alert channel when configured.
        Swallows every send error (logged) so callers can fire-and-forget."""
        log.log(_LEVEL.get(severity, logging.ERROR), "[notify] %s", message)
        if not self._channel or self._client is None:
            return
        try:
            await self._client.send_message(self._channel, f"[{severity.value}] {message}")
        except Exception:  # noqa: BLE001 - a failed alert must never propagate
            log.exception("[notify] could not send alert to channel %s", self._channel)
