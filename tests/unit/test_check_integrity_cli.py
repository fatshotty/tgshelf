from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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

    async def fake_run(
        db_dsn,
        path,
        *,
        depth,
        verify_telegram,
        deep_telegram,
        include_bots,
        config_path,
        runtime_config,
    ):
        calls["db_dsn"] = db_dsn
        calls["path"] = path
        calls["depth"] = depth
        calls["verify_telegram"] = verify_telegram
        calls["deep_telegram"] = deep_telegram
        calls["include_bots"] = include_bots
        calls["config_path"] = config_path
        calls["runtime_config"] = runtime_config
        return [], module.Report()

    monkeypatch.setattr(module, "run", fake_run)

    rc = module.main(["/media", "--config", str(config_path), "--verify-telegram"])

    assert rc == 0
    assert calls["db_dsn"] == "postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf"
    assert calls["path"] == "/media"
    assert calls["verify_telegram"] is True
    assert calls["deep_telegram"] is False
    assert calls["include_bots"] is False
    assert calls["runtime_config"].telegram.rate_limit.calls == 7
    assert calls["runtime_config"].telegram.rate_limit.window == 2.5
    assert "integrity report: 0 file(s) checked, 0 with issues" in capsys.readouterr().out


def test_verify_telegram_allows_disabled_write_rate_limit(tmp_path, monkeypatch):
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
        return [], module.Report()

    monkeypatch.setattr(module, "run", fake_run)

    rc = module.main(["/media", "--config", str(config_path), "--verify-telegram"])

    assert rc == 0


@pytest.mark.asyncio
async def test_deep_telegram_report_captures_part_repair_details(capsys, tmp_path):
    module = load_check_integrity_module()
    node = SimpleNamespace(id="file1", name="Movie Renamed.mkv", size=30)
    parts = [
        SimpleNamespace(
            idx=1,
            channel_id=-100,
            message_id=101,
            doc_id=1001,
            size=10,
            original_filename="Movie Original.mkv.001",
        ),
        SimpleNamespace(
            idx=2,
            channel_id=-100,
            message_id=102,
            doc_id=1002,
            size=10,
            original_filename="Movie Original.mkv.002",
        ),
        SimpleNamespace(
            idx=3,
            channel_id=-100,
            message_id=103,
            doc_id=1003,
            size=10,
            original_filename="Movie Original.mkv.003",
        ),
    ]

    class FakeRepo:
        async def content_of(self, node_id):
            return None

        async def parts_of(self, node_id):
            return parts

        async def path_of(self, node_id):
            return "/media/Movie Renamed.mkv"

    class FakeGateway:
        async def get_document(self, channel_id, message_id):
            if message_id == 101:
                return None
            if message_id == 102:
                return SimpleNamespace(
                    doc_id=1002,
                    size=10,
                    caption="filename: Wrong Name.mkv.002",
                )
            return SimpleNamespace(
                doc_id=9999,
                size=9,
                caption="filename: Movie Original.mkv.003",
            )

    verdict = await module.check_file(
        FakeRepo(),
        node,
        probes=[module.TelegramProbe("user_main", FakeGateway())],
        deep_telegram=True,
    )

    assert verdict.path == "/media/Movie Renamed.mkv"
    assert [issue.code for issue in verdict.issues] == [
        "missing_message",
        "caption_mismatch",
        "doc_id_mismatch",
        "part_size_mismatch",
    ]

    report_path = tmp_path / "report.jsonl"
    module._print_report([verdict], module.Report(checked=1, bad=1))
    module._write_jsonl_report([verdict], report_path)
    out = capsys.readouterr().out

    assert "/media/Movie Renamed.mkv" in out
    assert "[missing_message]" in out
    assert "part: 1" in out
    assert "channel_id: -100" in out
    assert "message_id: 101" in out
    assert "caption_expected: filename: Movie Original.mkv.002" in out
    assert "caption_actual: filename: Wrong Name.mkv.002" in out
    assert "doc_id_db: 1003" in out
    assert "doc_id_tg: 9999" in out
    assert "size_db: 10" in out
    assert "size_tg: 9" in out

    rows = [json.loads(line) for line in report_path.read_text().splitlines()]
    assert rows[0]["issue"] == "missing_message"
    assert rows[0]["path"] == "/media/Movie Renamed.mkv"
    assert rows[0]["part_idx"] == 1
    assert rows[0]["clients_tried"] == ["user_main"]
    assert rows[1]["caption_expected"] == "filename: Movie Original.mkv.002"
