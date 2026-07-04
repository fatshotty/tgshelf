from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgshelf.commands.common import resolve_concurrent
from tgshelf.config import ConfigError, load_config


def config_with_concurrent(value: int):
    return SimpleNamespace(operations=SimpleNamespace(concurrent=value))


def test_resolve_concurrent_prefers_env_over_cli_and_config():
    value = resolve_concurrent(
        config_with_concurrent(2),
        cli_value=3,
        env={"CONCURRENCY": "4"},
    )

    assert value == 4


def test_resolve_concurrent_prefers_cli_over_config_when_env_missing():
    value = resolve_concurrent(
        config_with_concurrent(2),
        cli_value=3,
        env={},
    )

    assert value == 3


def test_resolve_concurrent_falls_back_to_config():
    value = resolve_concurrent(
        config_with_concurrent(2),
        cli_value=None,
        env={},
    )

    assert value == 2


def test_resolve_concurrent_rejects_invalid_values():
    with pytest.raises(ValueError, match="CONCURRENCY must be an integer >= 1"):
        resolve_concurrent(config_with_concurrent(2), cli_value=3, env={"CONCURRENCY": "nope"})

    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        resolve_concurrent(config_with_concurrent(2), cli_value=0, env={})


def write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_operations_actions_and_within_are_loaded(tmp_path):
    path = write_config(
        tmp_path,
        """
db: postgresql+asyncpg://user:pass@localhost/tgshelf
telegram:
  upload:
    channel: -100123
operations:
  concurrent: 4
  actions: 16
  within: 40
""",
    )

    config = load_config(path, env={})

    assert config.operations.concurrent == 4
    assert config.operations.actions == 16
    assert config.operations.within == 40.0


def test_legacy_telegram_rate_limit_is_rejected(tmp_path):
    path = write_config(
        tmp_path,
        """
db: postgresql+asyncpg://user:pass@localhost/tgshelf
telegram:
  upload:
    channel: -100123
  rate_limit:
    calls: 7
    window: 2.5
""",
    )

    with pytest.raises(ConfigError, match="telegram.rate_limit"):
        load_config(path, env={})


def test_legacy_operations_batch_keys_are_rejected(tmp_path):
    path = write_config(
        tmp_path,
        """
db: postgresql+asyncpg://user:pass@localhost/tgshelf
telegram:
  upload:
    channel: -100123
operations:
  concurrent: 4
  sleep: 1
  batch: 10
""",
    )

    with pytest.raises(ConfigError, match="operations.sleep"):
        load_config(path, env={})
