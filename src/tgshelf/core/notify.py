"""Best-effort alert delivery for grave runtime conditions.

The notifier always logs synchronously. When a Bot API token and destination
chat are configured, it can either send inline (for ad-hoc instances) or enqueue
messages to a background worker (the `serve` path) so callers never wait on the
network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import socket
import string
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiohttp

from tgshelf.config import NOTIFY_TEMPLATE
from tgshelf.telegram.errors import Severity

log = logging.getLogger("tgshelf.notify")

_LEVEL = {
    Severity.CRITICAL: logging.ERROR,
    Severity.ERROR: logging.ERROR,
    Severity.WARNING: logging.WARNING,
}

_STOP = object()
_FORMATTER = string.Formatter()

Sender = Callable[[str, int | str, str], Awaitable[None]]
AlertPayload = str | Mapping[str, Any]


async def _send_via_bot_api(bot_token: str, chat_id: int | str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={"chat_id": chat_id, "text": text}) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Bot API sendMessage failed with HTTP {resp.status}: {body}")


class Notifier:
    def __init__(
        self,
        *,
        bot_token: str | None = None,
        channel: int | str | None = None,
        template: str = NOTIFY_TEMPLATE,
        warning_window: float = 300.0,
        sender: Sender = _send_via_bot_api,
    ):
        self._bot_token = bot_token
        self._channel = channel
        self._template = template
        self._warning_window = warning_window
        self._sender = sender
        self._queue: asyncio.Queue[tuple[int | str, str] | object] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._warning_state: dict[str, tuple[float, int]] = {}

    async def start(self) -> None:
        if self._queue is not None:
            return
        queue: asyncio.Queue[tuple[int | str, str] | object] = asyncio.Queue()
        self._queue = queue
        self._worker = asyncio.create_task(self._run(queue), name="tgshelf-notifier")

    async def aclose(self) -> None:
        if self._queue is None:
            return
        await self._queue.put(_STOP)
        worker = self._worker
        self._queue = None
        self._worker = None
        if worker is not None:
            await worker

    async def notify(
        self,
        message: AlertPayload,
        *,
        severity: Severity = Severity.ERROR,
        key: str | None = None,
    ) -> None:
        """Log and optionally deliver an alert. This method never raises."""
        log.log(
            _LEVEL.get(severity, logging.ERROR), "[notify] %s", self._summary(message)
        )
        text = self._message_for_delivery(message, severity=severity, key=key)
        if text is None or not self._bot_token or self._channel is None:
            return

        queue = self._queue
        if queue is None:
            await self._send(self._channel, text)
            return
        await queue.put((self._channel, text))

    def _message_for_delivery(
        self, message: AlertPayload, *, severity: Severity, key: str | None
    ) -> str | None:
        suppressed = 0
        if severity is Severity.WARNING and key:
            now = time.monotonic()
            last_sent, count = self._warning_state.get(key, (0.0, 0))
            if last_sent and now - last_sent < self._warning_window:
                self._warning_state[key] = (last_sent, count + 1)
                return None
            suppressed = count
            self._warning_state[key] = (now, 0)

        text = self._render(message, severity=severity, key=key)
        if suppressed:
            text = f"{text} (+{suppressed} similar suppressed)"
        return text

    def _render(self, message: AlertPayload, *, severity: Severity, key: str | None) -> str:
        fields = self._fields(message, severity=severity, key=key)
        try:
            return _render_template(self._template, fields)
        except Exception:  # noqa: BLE001 - alerts must keep working with bad templates
            log.exception("[notify] invalid alert template; using fallback text")
            return f"[tgshelf:{severity.value}] {fields.get('title') or fields.get('message')}"

    def _fields(
        self, message: AlertPayload, *, severity: Severity, key: str | None
    ) -> dict[str, Any]:
        if isinstance(message, Mapping):
            fields = {str(k): v for k, v in message.items() if _present(v)}
        else:
            text = str(message)
            fields = {"message": text, "title": text}

        if _present(fields.get("message")) and not _present(fields.get("title")):
            fields["title"] = fields["message"]
        if _present(fields.get("title")) and not _present(fields.get("message")):
            fields["message"] = fields["title"]

        fields["severity"] = severity.value
        if key:
            fields["key"] = key
        fields.setdefault(
            "time", dt.datetime.now().astimezone().isoformat(timespec="seconds")
        )
        fields.setdefault("host", socket.gethostname())
        return fields

    def _summary(self, message: AlertPayload) -> str:
        if isinstance(message, Mapping):
            for field in ("title", "message", "cause"):
                value = message.get(field)
                if _present(value):
                    return str(value)
            return str(dict(message))
        return str(message)

    async def _run(self, queue: asyncio.Queue[tuple[int | str, str] | object]) -> None:
        while True:
            item = await queue.get()
            try:
                if item is _STOP:
                    return
                channel, text = item
                await self._send(channel, text)
            finally:
                queue.task_done()

    async def _send(self, channel: int | str, text: str) -> None:
        if not self._bot_token:
            return
        try:
            await self._sender(self._bot_token, channel, text)
        except Exception:  # noqa: BLE001 - a failed alert path must never propagate
            log.exception("[notify] could not send alert to channel %s", channel)


def _render_template(template: str, fields: Mapping[str, Any]) -> str:
    rendered: list[str] = []
    for raw_line in template.splitlines():
        if raw_line == "":
            if rendered and rendered[-1] != "":
                rendered.append("")
            continue

        names = _field_names(raw_line)
        if any(not _present(fields.get(name)) for name in names):
            continue
        rendered.append(raw_line.format_map(fields))

    while rendered and rendered[-1] == "":
        rendered.pop()
    return "\n".join(rendered)


def _field_names(template_line: str) -> set[str]:
    names: set[str] = set()
    for _, field_name, _, _ in _FORMATTER.parse(template_line):
        if field_name:
            root = field_name.split(".", 1)[0].split("[", 1)[0]
            names.add(root)
    return names


def _present(value: Any) -> bool:
    return value is not None and str(value) != ""
