from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def load_check_integrity_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_integrity.py"
    spec = importlib.util.spec_from_file_location("check_integrity_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_main_uses_config_db_and_rate_limits_when_db_env_is_missing(
    tmp_path, monkeypatch, capsys
):
    module = load_check_integrity_module()
    monkeypatch.delenv("DB", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
db: postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf
telegram:
  upload:
    channel: -100123
  rate_limit:
    calls: 7
    window: 2.5
""",
        encoding="utf-8",
    )
    calls = {}

    async def fake_run(db_dsn, path, *, depth, verify_telegram, config_path, runtime_config):
        calls["db_dsn"] = db_dsn
        calls["path"] = path
        calls["depth"] = depth
        calls["verify_telegram"] = verify_telegram
        calls["config_path"] = config_path
        calls["runtime_config"] = runtime_config
        return [], module.Report()

    monkeypatch.setattr(module, "run", fake_run)

    rc = module.main(["/media", "--config", str(config_path), "--verify-telegram"])

    assert rc == 0
    assert calls["db_dsn"] == "postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf"
    assert calls["path"] == "/media"
    assert calls["verify_telegram"] is True
    assert calls["runtime_config"].telegram.rate_limit.calls == 7
    assert calls["runtime_config"].telegram.rate_limit.window == 2.5
    assert "integrity report: 0 file(s) checked, 0 with issues" in capsys.readouterr().out


def test_verify_telegram_requires_enabled_rate_limit(tmp_path, monkeypatch, capsys):
    module = load_check_integrity_module()
    monkeypatch.delenv("DB", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
db: postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf
telegram:
  upload:
    channel: -100123
  rate_limit:
    calls: 0
    window: 1.0
""",
        encoding="utf-8",
    )

    async def fake_run(*args, **kwargs):
        raise AssertionError("run should not start without an enabled Telegram rate limit")

    monkeypatch.setattr(module, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        module.main(["/media", "--config", str(config_path), "--verify-telegram"])

    assert exc.value.code == 2
    assert "telegram.rate_limit.calls > 0" in capsys.readouterr().err
