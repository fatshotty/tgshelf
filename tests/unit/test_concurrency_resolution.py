from __future__ import annotations

from types import SimpleNamespace

import pytest

from tgshelf.commands.common import resolve_concurrent


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
