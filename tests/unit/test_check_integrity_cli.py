from __future__ import annotations

import importlib.util
import json
import asyncio
import logging
import sys
import time
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
operations:
  concurrent: 7
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
        concurrent,
        config_path,
        runtime_config,
        progress_every,
    ):
        calls["db_dsn"] = db_dsn
        calls["path"] = path
        calls["depth"] = depth
        calls["verify_telegram"] = verify_telegram
        calls["deep_telegram"] = deep_telegram
        calls["include_bots"] = include_bots
        calls["concurrent"] = concurrent
        calls["config_path"] = config_path
        calls["runtime_config"] = runtime_config
        calls["progress_every"] = progress_every
        return [], module.Report()

    monkeypatch.setattr(module, "run", fake_run)

    rc = module.main(["/media", "--config", str(config_path), "--verify-telegram"])

    assert rc == 0
    assert calls["db_dsn"] == "postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf"
    assert calls["path"] == "/media"
    assert calls["verify_telegram"] is True
    assert calls["deep_telegram"] is False
    assert calls["include_bots"] is False
    assert calls["concurrent"] == 7
    assert calls["progress_every"] == 100
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


@pytest.mark.asyncio
async def test_telegram_part_checks_respect_concurrency_and_spread_probe_starts():
    module = load_check_integrity_module()
    active = 0
    max_active = 0
    used = []

    class SlowGateway:
        def __init__(self, name):
            self.name = name

        async def get_document(self, channel_id, message_id):
            nonlocal active, max_active
            used.append(self.name)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return SimpleNamespace(
                doc_id=message_id,
                size=10,
                caption=f"filename: file.{message_id:03d}",
            )

    parts = [
        SimpleNamespace(
            idx=i,
            channel_id=-100,
            message_id=i,
            doc_id=i,
            size=10,
            original_filename=f"file.{i:03d}",
        )
        for i in range(1, 5)
    ]
    probes = [module.TelegramProbe(f"account_{i}", SlowGateway(f"account_{i}")) for i in range(1, 5)]
    started = time.perf_counter()

    issues = await module.verify_telegram_parts(probes, parts, deep=True, concurrent=4)

    assert issues == []
    assert max_active == 4
    assert time.perf_counter() - started < 0.06
    assert used == ["account_1", "account_2", "account_3", "account_4"]


@pytest.mark.asyncio
async def test_check_concurrency_applies_across_files_not_only_parts():
    module = load_check_integrity_module()
    active = 0
    max_active = 0
    root = SimpleNamespace(id="root", name="media", is_folder=True, size=0)
    files = [
        SimpleNamespace(id="file1", name="one.mkv", is_folder=False, size=10),
        SimpleNamespace(id="file2", name="two.mkv", is_folder=False, size=10),
    ]
    parts = {
        "file1": [SimpleNamespace(
            idx=1, channel_id=-100, message_id=1, doc_id=1, size=10,
            original_filename="one.mkv.001",
        )],
        "file2": [SimpleNamespace(
            idx=1, channel_id=-100, message_id=2, doc_id=2, size=10,
            original_filename="two.mkv.001",
        )],
    }

    class FakeRepo:
        async def children(self, node_id):
            return files if node_id == "root" else []

        async def content_of(self, node_id):
            return None

        async def parts_of(self, node_id):
            return parts[node_id]

        async def path_of(self, node_id):
            return f"/media/{node_id}.mkv"

    class SlowGateway:
        async def get_document(self, channel_id, message_id):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return SimpleNamespace(
                doc_id=message_id,
                size=10,
                caption=f"filename: {'one' if message_id == 1 else 'two'}.mkv.001",
            )

    started = time.perf_counter()
    verdicts, report = await module.check(
        FakeRepo(),
        root,
        probes=[module.TelegramProbe("account_1", SlowGateway())],
        deep_telegram=True,
        concurrent=2,
    )

    assert report.checked == 2
    assert report.bad == 0
    assert [v.ok for v in verdicts] == [True, True]
    assert max_active == 2
    assert time.perf_counter() - started < 0.04


@pytest.mark.asyncio
async def test_check_logs_walk_and_telegram_progress(caplog):
    module = load_check_integrity_module()
    root = SimpleNamespace(id="root", name="media", is_folder=True, size=0)
    files = [
        SimpleNamespace(id="file1", name="one.mkv", is_folder=False, size=10),
        SimpleNamespace(id="file2", name="two.mkv", is_folder=False, size=10),
    ]
    parts = {
        "file1": [SimpleNamespace(
            idx=1, channel_id=-100, message_id=1, doc_id=1, size=10,
            original_filename="one.mkv.001",
        )],
        "file2": [SimpleNamespace(
            idx=1, channel_id=-100, message_id=2, doc_id=2, size=10,
            original_filename="two.mkv.001",
        )],
    }

    class FakeRepo:
        async def children(self, node_id):
            return files if node_id == "root" else []

        async def content_of(self, node_id):
            return None

        async def parts_of(self, node_id):
            return parts[node_id]

        async def path_of(self, node_id):
            return f"/media/{node_id}.mkv"

    class Gateway:
        async def get_document(self, channel_id, message_id):
            return SimpleNamespace(
                doc_id=message_id,
                size=10,
                caption=f"filename: {'one' if message_id == 1 else 'two'}.mkv.001",
            )

    caplog.set_level(logging.INFO, logger="tgshelf.check")

    verdicts, report = await module.check(
        FakeRepo(),
        root,
        probes=[module.TelegramProbe("account_1", Gateway())],
        deep_telegram=True,
        concurrent=1,
        progress_every=1,
    )

    assert report.checked == 2
    assert [v.ok for v in verdicts] == [True, True]
    messages = [record.getMessage() for record in caplog.records]
    assert "[check] walking files under 'media' depth=0" in messages
    assert "[check] db scan complete: 2 file(s), 2 telegram part(s)" in messages
    assert "[check] telegram progress: 1/2 part(s)" in messages
    assert "[check] telegram progress: 2/2 part(s)" in messages
    assert "[check] complete: 2 file(s) checked, 0 with issues" in messages


@pytest.mark.asyncio
async def test_check_logs_only_folders_with_channel_override(caplog):
    module = load_check_integrity_module()
    root = SimpleNamespace(id="root", name="media", is_folder=True, size=0, channel_id=None)
    movies = SimpleNamespace(id="movies", name="movies", is_folder=True, size=0, channel_id=-1001)
    inception = SimpleNamespace(id="inception", name="Inception (2010)", is_folder=True, size=0, channel_id=None)
    file_node = SimpleNamespace(id="file1", name="movie.mkv", is_folder=False, size=3, channel_id=-1001)

    class FakeRepo:
        async def children(self, node_id):
            return {
                "root": [movies],
                "movies": [inception],
                "inception": [file_node],
            }.get(node_id, [])

        async def content_of(self, node_id):
            return b"abc"

        async def parts_of(self, node_id):
            return []

        async def path_of(self, node_id):
            return {
                "root": "/media",
                "movies": "/media/movies",
                "inception": "/media/movies/Inception (2010)",
                "file1": "/media/movies/Inception (2010)/movie.mkv",
            }[node_id]

    caplog.set_level(logging.INFO, logger="tgshelf.check")

    verdicts, report = await module.check(
        FakeRepo(),
        root,
        depth=0,
        progress_every=1,
    )

    assert report.checked == 1
    assert [v.path for v in verdicts] == ["/media/movies/Inception (2010)/movie.mkv"]
    messages = [record.getMessage() for record in caplog.records]
    assert "[check] processing channel folder: /media/movies (channel_id=-1001)" in messages
    assert not any("Inception (2010) (channel_id=" in message for message in messages)


@pytest.mark.asyncio
async def test_check_processes_channel_folders_sequentially(caplog):
    module = load_check_integrity_module()
    root = SimpleNamespace(id="root", name="media", is_folder=True, size=0, channel_id=None)
    animation = SimpleNamespace(id="animation", name="animation", is_folder=True, size=0, channel_id=-1001)
    movies = SimpleNamespace(id="movies", name="movies", is_folder=True, size=0, channel_id=-1002)
    animation_file = SimpleNamespace(id="animation-file", name="anim.mkv", is_folder=False, size=10, channel_id=-1001)
    movies_file = SimpleNamespace(id="movies-file", name="movie.mkv", is_folder=False, size=10, channel_id=-1002)
    parts = {
        "animation-file": [SimpleNamespace(
            idx=1, channel_id=-1001, message_id=11, doc_id=11, size=10,
            original_filename="anim.mkv.001",
        )],
        "movies-file": [SimpleNamespace(
            idx=1, channel_id=-1002, message_id=22, doc_id=22, size=10,
            original_filename="movie.mkv.001",
        )],
    }

    class FakeRepo:
        async def children(self, node_id):
            return {
                "root": [animation, movies],
                "animation": [animation_file],
                "movies": [movies_file],
            }.get(node_id, [])

        async def content_of(self, node_id):
            return None

        async def parts_of(self, node_id):
            return parts[node_id]

        async def path_of(self, node_id):
            return {
                "root": "/media",
                "animation": "/media/animation",
                "movies": "/media/movies",
                "animation-file": "/media/animation/anim.mkv",
                "movies-file": "/media/movies/movie.mkv",
            }[node_id]

    class Gateway:
        async def get_document(self, channel_id, message_id):
            return SimpleNamespace(
                doc_id=message_id,
                size=10,
                caption="filename: anim.mkv.001" if message_id == 11 else "filename: movie.mkv.001",
            )

    caplog.set_level(logging.INFO, logger="tgshelf.check")

    verdicts, report = await module.check(
        FakeRepo(),
        root,
        probes=[module.TelegramProbe("account_1", Gateway())],
        deep_telegram=True,
        concurrent=2,
        progress_every=1,
    )

    assert report.checked == 2
    assert [v.path for v in verdicts] == [
        "/media/animation/anim.mkv",
        "/media/movies/movie.mkv",
    ]
    messages = [record.getMessage() for record in caplog.records]
    animation_start = messages.index("[check] processing channel folder: /media/animation (channel_id=-1001)")
    first_done = messages.index("[check] telegram progress: 1/1 part(s)")
    movies_start = messages.index("[check] processing channel folder: /media/movies (channel_id=-1002)")
    assert animation_start < first_done < movies_start
