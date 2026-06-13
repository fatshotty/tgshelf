"""Error taxonomy with severities.

Every grave condition the system can hit has a typed error here, each with a
severity and a human-oriented message (cause + remedy) — this is what feeds
the Notifier (log + Telegram alert channel). `translate_telethon_error` is the
single point where raw Telethon exceptions become domain errors: engines never
import telethon error types.
"""

from __future__ import annotations

import enum

from telethon import errors as tg_errors


class Severity(enum.Enum):
    CRITICAL = "critical"  # data integrity / whole subtree unavailable
    ERROR = "error"        # an account/bot is out of service
    WARNING = "warning"    # transient, aggregated/deduped by the Notifier


class TgError(Exception):
    severity: Severity = Severity.ERROR


class FloodCooldown(TgError):
    """Telegram asked to slow down: the client must cool down for `seconds`."""

    severity = Severity.WARNING

    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"flood wait: cool down for {seconds}s")


class FileRefExpired(TgError):
    """The cached file location expired (~30min): re-fetch the message and retry."""

    severity = Severity.WARNING

    def __init__(self) -> None:
        super().__init__("file reference expired: re-resolve the document and retry")


class SessionDead(TgError):
    """The session was revoked/unregistered: account out of the pool, re-login needed."""

    severity = Severity.ERROR

    def __init__(self, detail: str = ""):
        super().__init__(
            f"session dead ({detail or 'auth key unregistered/revoked'}): "
            "account removed from pool, run `tgshelf accounts login <name>`"
        )


class ChannelUnavailable(TgError):
    """The client cannot access the channel (bot not a member / kicked)."""

    severity = Severity.ERROR

    def __init__(self, detail: str = ""):
        super().__init__(
            f"channel unavailable{f' ({detail})' if detail else ''}: "
            "client is not a member; run `tgshelf bots check` to repair"
        )


class PartMissing(TgError):
    """A Telegram message holding a file part is gone: integrity violated."""

    severity = Severity.CRITICAL

    def __init__(self, file_path: str, part_idx: int):
        self.file_path = file_path
        self.part_idx = part_idx
        super().__init__(
            f"part {part_idx} of '{file_path}' is missing on Telegram "
            "(message deleted from the channel): file is not fully downloadable"
        )


class DocIdMismatch(TgError):
    """The live message does not hold the expected document: DB/channel drift."""

    severity = Severity.CRITICAL

    def __init__(self, expected: int, found: int | None):
        super().__init__(
            f"doc_id mismatch (expected {expected}, found {found}): "
            "refusing to touch a message that no longer holds our file"
        )


_SESSION_DEAD = (
    tg_errors.AuthKeyUnregisteredError,
    tg_errors.SessionRevokedError,
    tg_errors.UserDeactivatedError,
    tg_errors.UserDeactivatedBanError,
)

_CHANNEL_UNAVAILABLE = (
    tg_errors.ChannelPrivateError,
    tg_errors.ChatForbiddenError,
)


def translate_telethon_error(exc: BaseException) -> TgError | None:
    """Map a raw Telethon exception to a domain error; None = not ours."""
    if isinstance(exc, tg_errors.FloodWaitError):
        return FloodCooldown(exc.seconds)
    if isinstance(exc, tg_errors.FileReferenceExpiredError):
        return FileRefExpired()
    if isinstance(exc, _SESSION_DEAD):
        return SessionDead(type(exc).__name__)
    if isinstance(exc, _CHANNEL_UNAVAILABLE):
        return ChannelUnavailable(type(exc).__name__)
    return None
