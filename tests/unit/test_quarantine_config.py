from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgshelf.config import ConfigError, load_config
from tgshelf.http.serve import build_pools


def write_config(tmp_path, download: str = ""):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
db: postgresql+asyncpg://user:pass@localhost/tgshelf
telegram:
  upload:
    channel: -100123
download:
"""
        + download,
        encoding="utf-8",
    )
    return path


def test_download_quarantine_backoff_defaults_to_five_through_sixty_minutes(
    tmp_path,
) -> None:
    config = load_config(write_config(tmp_path), env={})

    assert config.download.quarantine_backoff_seconds == (
        300.0,
        900.0,
        1800.0,
        3600.0,
    )


def test_download_quarantine_backoff_loads_positive_sequence(tmp_path) -> None:
    config = load_config(
        write_config(tmp_path, "  quarantine_backoff_seconds: [30, 60, 120]\n"),
        env={},
    )

    assert config.download.quarantine_backoff_seconds == (30.0, 60.0, 120.0)


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        "[300, 0]",
        "[300, -1]",
        "[300, 60]",
        '"300,900"',
        "[.nan]",
        "[.inf]",
        "[true]",
    ],
)
def test_download_quarantine_backoff_rejects_invalid_sequences(
    tmp_path, value: str
) -> None:
    path = write_config(tmp_path, f"  quarantine_backoff_seconds: {value}\n")

    with pytest.raises(ConfigError, match="download.quarantine_backoff_seconds"):
        load_config(path, env={})


def test_build_pools_applies_configured_quarantine_backoff() -> None:
    user = SimpleNamespace(name="user", is_bot=False)
    bot = SimpleNamespace(name="bot", is_bot=True)
    _clients, bots = build_pools(
        [(user, object()), (bot, object())],
        quarantine_backoff=(12.0,),
    )
    member = bots.members[0]

    bots.mark_error(member)
    bots.mark_error(member)
    bots.mark_error(member)

    assert bots.next_quarantine_wait() == pytest.approx(12.0, abs=0.1)
