"""`tgshelf create-bots` and `tgshelf bots check`.

Two admin commands sharing the same building blocks (port of the legacy
`python/commands/create_bots.py`):

- **create-bots**: drives a BotFather conversation through a user account to
  create N bots, then promotes each to read-only admin of selected channels
  (promoting = adding). Interactive (account + channel selection), like legacy.
- **bots check**: for every configured bot and every channel in use, verifies
  membership via the user account and repairs (re-promotes) what is missing.

There is no request/response API with BotFather: we send a message and poll the
chat history for a newer reply (`send_and_wait`). The pure helpers below (naming,
token extraction, reply classification) are unit-tested; the conversation and the
promotion touch real Telegram and are covered by manual smoke.
"""

from __future__ import annotations

import re

BOTFATHER = "BotFather"

# BotFather token: "<8-12 digits>:<35+ url-safe chars>" (legacy create_bots.py:166)
_TOKEN_RE = re.compile(r"(\d{8,12}:[A-Za-z0-9_-]{35,})")

# classify_botfather_reply outcomes
REPLY_OK = "ok"
REPLY_BUSY = "busy"  # a /newbot dialog is already open -> /cancel and retry
REPLY_REJECTED = "rejected"  # the chosen username was refused


def bot_username(prefix: str, n: int) -> str:
    """Deterministic bot username: `{prefix}_{NN}_bot`, zero-padded to 2 digits
    (legacy naming, e.g. redstream_07_bot)."""
    return f"{prefix}_{n:02d}_bot"


def extract_bot_token(text: str | None) -> str | None:
    """Pull a bot token out of a BotFather reply, or None if absent."""
    match = _TOKEN_RE.search(text or "")
    return match.group(1) if match else None


def classify_botfather_reply(text: str | None) -> str:
    """Read a BotFather reply: REPLY_BUSY when an earlier /newbot is still open
    ('already have'/'use /cancel'), REPLY_REJECTED when the username was refused
    ('sorry'/'already taken'/'invalid'), otherwise REPLY_OK."""
    low = (text or "").lower()
    if "already have" in low or "use /cancel" in low:
        return REPLY_BUSY
    if "sorry" in low or "already taken" in low or "invalid" in low:
        return REPLY_REJECTED
    return REPLY_OK
