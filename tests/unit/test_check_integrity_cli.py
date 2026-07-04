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


def load_sanitize_captions_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "sanitize_captions.py"
    spec = importlib.util.spec_from_file_location("sanitize_captions_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_main_uses_config_db_and_write_bucket_when_db_env_is_missing(
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
operations:
  concurrent: 7
  actions: 16
  within: 40
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
    assert calls["runtime_config"].operations.actions == 16
    assert calls["runtime_config"].operations.within == 40
    assert "integrity report: 0 file(s) checked, 0 with issues" in capsys.readouterr().out


def test_verify_telegram_allows_disabled_write_bucket(tmp_path, monkeypatch):
    module = load_check_integrity_module()
    monkeypatch.delenv("DB", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
db: postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf
telegram:
  upload:
    channel: -100123
operations:
  actions: 0
""",
        encoding="utf-8",
    )

    async def fake_run(*args, **kwargs):
        return [], module.Report()

    monkeypatch.setattr(module, "run", fake_run)

    rc = module.main(["/media", "--config", str(config_path), "--verify-telegram"])

    assert rc == 0


@pytest.mark.asyncio
async def test_sanitize_dry_run_reports_caption_and_original_filename_drift():
    module = load_sanitize_captions_module()
    node = SimpleNamespace(id="file1", name="Movie.mkv", size=20)
    parts = [
        SimpleNamespace(
            file_id="file1",
            idx=0,
            channel_id=-100,
            message_id=101,
            doc_id=1001,
            size=10,
            original_filename="Old physical.mkv.001",
        ),
        SimpleNamespace(
            file_id="file1",
            idx=1,
            channel_id=-100,
            message_id=102,
            doc_id=1002,
            size=10,
            original_filename="Movie physical.mkv.002",
        ),
    ]

    class FakeRepo:
        def __init__(self):
            self.renamed = []

        async def content_of(self, node_id):
            return None

        async def parts_of(self, node_id):
            return parts

        async def path_of(self, node_id):
            return "/media/Movie.mkv"

        async def set_part_original_filename(self, file_id, idx, original_filename):
            self.renamed.append((file_id, idx, original_filename))

    class FakeGateway:
        def __init__(self):
            self.edits = []

        async def get_document(self, channel_id, message_id):
            return SimpleNamespace(
                doc_id=1000 + message_id - 100,
                size=10,
                filename=f"Movie physical.mkv.{message_id - 100:03d}",
                caption=f"fileName: Old.mkv.{message_id - 100:03d}",
            )

        async def edit_message_caption(self, channel_id, message_id, caption):
            self.edits.append((channel_id, message_id, caption))

    repo = FakeRepo()
    gateway = FakeGateway()

    result = await module.sanitize_file(repo, node, gateway, dry_run=True)

    assert result.checked == 1
    assert result.parts == 2
    assert result.caption_updates == 2
    assert result.original_filename_updates == 1
    assert result.errors == 0
    assert gateway.edits == []
    assert repo.renamed == []
    assert [record.status for record in result.records] == ["dry_run", "dry_run"]
    assert result.records[0].caption_expected == "fileName: Movie.mkv.001"
    assert result.records[0].original_filename_tg == "Movie physical.mkv.001"


@pytest.mark.asyncio
async def test_sanitize_apply_edits_caption_and_updates_original_filename():
    module = load_sanitize_captions_module()
    node = SimpleNamespace(id="file1", name="Movie.mkv", size=10)
    part = SimpleNamespace(
        file_id="file1",
        idx=0,
        channel_id=-100,
        message_id=101,
        doc_id=1001,
        size=10,
        original_filename="Old physical.mkv",
    )

    class FakeRepo:
        def __init__(self):
            self.renamed = []

        async def content_of(self, node_id):
            return None

        async def parts_of(self, node_id):
            return [part]

        async def path_of(self, node_id):
            return "/media/Movie.mkv"

        async def set_part_original_filename(self, file_id, idx, original_filename):
            self.renamed.append((file_id, idx, original_filename))

    class FakeGateway:
        def __init__(self):
            self.edits = []

        async def get_document(self, channel_id, message_id):
            return SimpleNamespace(
                doc_id=1001,
                size=10,
                filename="Movie physical.mkv",
                caption="fileName: Old.mkv",
            )

        async def edit_message_caption(self, channel_id, message_id, caption):
            self.edits.append((channel_id, message_id, caption))

    repo = FakeRepo()
    gateway = FakeGateway()

    result = await module.sanitize_file(repo, node, gateway, dry_run=False)

    assert result.caption_updates == 1
    assert result.original_filename_updates == 1
    assert result.errors == 0
    assert gateway.edits == [(-100, 101, "fileName: Movie.mkv")]
    assert repo.renamed == [("file1", 0, "Movie physical.mkv")]
    assert result.records[0].status == "updated"


@pytest.mark.asyncio
async def test_sanitize_round_robin_gateway_rotates_accounts(caplog):
    module = load_sanitize_captions_module()

    class FakeGateway:
        def __init__(self, name):
            self.name = name
            self.calls = []

        async def get_document(self, channel_id, message_id):
            self.calls.append((channel_id, message_id))
            return self.name

    main = FakeGateway("main")
    alt = FakeGateway("alt")
    gateway = module.RoundRobinGateway([
        ("main", main),
        ("alt", alt),
    ])
    caplog.set_level(logging.INFO, logger="tgshelf.sanitize")

    assert await gateway.get_document(-100, 1) == "main"
    assert await gateway.get_document(-100, 2) == "alt"
    assert await gateway.get_document(-100, 3) == "main"

    assert main.calls == [(-100, 1), (-100, 3)]
    assert alt.calls == [(-100, 2)]
    messages = [record.getMessage() for record in caplog.records]
    assert "[sanitize] using account main" in messages
    assert "[sanitize] using account alt" in messages


@pytest.mark.asyncio
async def test_sanitize_round_robin_gateway_logs_flood_and_tries_next_account(caplog):
    module = load_sanitize_captions_module()
    from tgshelf.telegram.errors import FloodCooldown

    class FloodingGateway:
        def __init__(self):
            self.calls = 0

        async def get_document(self, channel_id, message_id):
            self.calls += 1
            raise FloodCooldown(20)

    class HealthyGateway:
        def __init__(self):
            self.calls = []

        async def get_document(self, channel_id, message_id):
            self.calls.append((channel_id, message_id))
            return "ok"

    main = FloodingGateway()
    alt = HealthyGateway()
    gateway = module.RoundRobinGateway([
        ("main", main),
        ("alt", alt),
    ])
    caplog.set_level(logging.INFO, logger="tgshelf.sanitize")

    assert await gateway.get_document(-100, 101) == "ok"

    assert main.calls == 1
    assert alt.calls == [(-100, 101)]
    messages = [record.getMessage() for record in caplog.records]
    assert "[sanitize] using account main" in messages
    assert "[sanitize] account main in flood_wait 20s" in messages
    assert "[sanitize] using account alt" in messages


@pytest.mark.asyncio
async def test_sanitize_build_gateway_uses_all_user_accounts(monkeypatch):
    module = load_sanitize_captions_module()
    import tgshelf.http.serve as serve

    main_gateway = object()
    bot_gateway = object()
    alt_gateway = object()
    pairs = [
        (SimpleNamespace(name="main", is_bot=False), main_gateway),
        (SimpleNamespace(name="bot01", is_bot=True), bot_gateway),
        (SimpleNamespace(name="alt", is_bot=False), alt_gateway),
    ]

    async def fake_start_clients(config, write_limiter):
        return pairs

    monkeypatch.setattr(serve, "make_write_limiter", lambda operations: "rl")
    monkeypatch.setattr(serve, "start_clients", fake_start_clients)

    gateway, clients = await module._build_gateway(
        "unused",
        runtime_config=SimpleNamespace(
            operations=SimpleNamespace(actions=0, within=40)
        ),
    )

    assert gateway.account_names == ["main", "alt"]
    assert clients == [main_gateway, bot_gateway, alt_gateway]


@pytest.mark.asyncio
async def test_sanitize_skips_writes_when_identity_checks_fail():
    module = load_sanitize_captions_module()
    node = SimpleNamespace(id="file1", name="Movie.mkv", size=10)
    part = SimpleNamespace(
        file_id="file1",
        idx=0,
        channel_id=-100,
        message_id=101,
        doc_id=1001,
        size=10,
        original_filename="Movie physical.mkv",
    )

    class FakeRepo:
        async def content_of(self, node_id):
            return None

        async def parts_of(self, node_id):
            return [part]

        async def path_of(self, node_id):
            return "/media/Movie.mkv"

        async def set_part_original_filename(self, file_id, idx, original_filename):
            raise AssertionError("must not update suspicious part")

    class FakeGateway:
        async def get_document(self, channel_id, message_id):
            return SimpleNamespace(
                doc_id=9999,
                size=10,
                filename="Movie physical.mkv",
                caption="fileName: Old.mkv",
            )

        async def edit_message_caption(self, channel_id, message_id, caption):
            raise AssertionError("must not edit suspicious part")

    result = await module.sanitize_file(FakeRepo(), node, FakeGateway(), dry_run=False)

    assert result.errors == 1
    assert result.caption_updates == 0
    assert result.original_filename_updates == 0
    assert result.records[0].status == "error"
    assert result.records[0].error == "doc_id_mismatch"


def test_sanitize_cli_defaults_to_apply(tmp_path, monkeypatch, capsys):
    module = load_sanitize_captions_module()
    monkeypatch.delenv("DB", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
db: postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf
telegram:
  upload:
    channel: -100123
operations:
  concurrent: 3
""",
        encoding="utf-8",
    )
    calls = {}

    async def fake_run(
        db_dsn,
        path,
        *,
        depth,
        dry_run,
        concurrent,
        config_path,
        runtime_config,
        progress_every,
    ):
        calls["db_dsn"] = db_dsn
        calls["path"] = path
        calls["depth"] = depth
        calls["dry_run"] = dry_run
        calls["concurrent"] = concurrent
        calls["config_path"] = config_path
        calls["runtime_config"] = runtime_config
        calls["progress_every"] = progress_every
        return module.SanitizeReport(checked=1, parts=2, caption_updates=1)

    monkeypatch.setattr(module, "run", fake_run)

    rc = module.main(["/media", "--config", str(config_path)])

    assert rc == 0
    assert calls["db_dsn"] == "postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf"
    assert calls["path"] == "/media"
    assert calls["dry_run"] is False
    assert calls["concurrent"] == 3
    assert calls["progress_every"] == 100
    assert "sanitize report: 1 file(s), 2 part(s), 1 caption update(s)" in capsys.readouterr().out


def test_sanitize_cli_accepts_explicit_dry_run(tmp_path, monkeypatch):
    module = load_sanitize_captions_module()
    monkeypatch.delenv("DB", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
db: postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf
telegram:
  upload:
    channel: -100123
""",
        encoding="utf-8",
    )
    calls = {}

    async def fake_run(*args, **kwargs):
        calls["dry_run"] = kwargs["dry_run"]
        return module.SanitizeReport()

    monkeypatch.setattr(module, "run", fake_run)

    rc = module.main(["/media", "--dry-run", "--config", str(config_path)])

    assert rc == 0
    assert calls["dry_run"] is True


def test_sanitize_writes_jsonl_report(tmp_path):
    module = load_sanitize_captions_module()
    report = module.SanitizeReport(
        checked=1,
        parts=1,
        caption_updates=1,
        records=[
            module.SanitizeRecord(
                path="/media/Movie.mkv",
                node_id="file1",
                name="Movie.mkv",
                part_idx=0,
                channel_id=-100,
                message_id=101,
                doc_id_db=1001,
                caption_expected="fileName: Movie.mkv",
                caption_actual="fileName: Old.mkv",
                caption_changed=True,
                status="updated",
            )
        ],
    )
    path = tmp_path / "sanitize.jsonl"

    module._write_jsonl_report(report, path)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["path"] == "/media/Movie.mkv"
    assert rows[0]["caption_changed"] is True
    assert rows[0]["status"] == "updated"


def test_sanitize_progress_percent_uses_integer_completion():
    module = load_sanitize_captions_module()

    assert module._progress_percent(1, 2) == 50
    assert module._progress_percent(2, 3) == 66
    assert module._progress_percent(3, 3) == 100


@pytest.mark.asyncio
async def test_sanitize_apply_report_replays_planned_writes(tmp_path, caplog):
    module = load_sanitize_captions_module()
    report_path = tmp_path / "dry-run.jsonl"
    record = module.SanitizeRecord(
        path="/media/Movie.mkv",
        node_id="file1",
        name="Movie.mkv",
        part_idx=0,
        channel_id=-100,
        message_id=101,
        doc_id_db=1001,
        doc_id_tg=1001,
        size_db=10,
        size_tg=10,
        caption_expected="fileName: Movie.mkv",
        caption_actual="fileName: Old.mkv",
        original_filename_db="Old physical.mkv",
        original_filename_tg="Movie physical.mkv",
        caption_changed=True,
        original_filename_changed=True,
        status="dry_run",
    )
    module._write_jsonl_report(module.SanitizeReport(records=[record]), report_path)
    part = SimpleNamespace(
        file_id="file1",
        idx=0,
        channel_id=-100,
        message_id=101,
        doc_id=1001,
        size=10,
        original_filename="Old physical.mkv",
    )

    class FakeSession:
        def __init__(self):
            self.commits = 0

        async def commit(self):
            self.commits += 1

    class FakeRepo:
        def __init__(self):
            self.session = FakeSession()
            self.renamed = []

        async def parts_of(self, file_id):
            return [part] if file_id == "file1" else []

        async def set_part_original_filename(self, file_id, idx, original_filename):
            self.renamed.append((file_id, idx, original_filename))

    class FakeGateway:
        def __init__(self):
            self.edits = []

        async def edit_message_caption(self, channel_id, message_id, caption):
            self.edits.append((channel_id, message_id, caption))

    repo = FakeRepo()
    gateway = FakeGateway()

    caplog.set_level(logging.INFO, logger="tgshelf.sanitize")

    result = await module.apply_report(
        repo, gateway, report_path, progress_every=1
    )

    assert result.parts == 1
    assert result.caption_updates == 1
    assert result.original_filename_updates == 1
    assert result.errors == 0
    assert gateway.edits == [(-100, 101, "fileName: Movie.mkv")]
    assert repo.renamed == [("file1", 0, "Movie physical.mkv")]
    assert repo.session.commits == 1
    assert result.records[0].status == "updated"
    messages = [record.getMessage() for record in caplog.records]
    assert "[sanitize] apply-report progress: 1/1 part(s), 100% completed" in messages


@pytest.mark.asyncio
async def test_sanitize_apply_report_skips_stale_db_rows(tmp_path):
    module = load_sanitize_captions_module()
    report_path = tmp_path / "dry-run.jsonl"
    record = module.SanitizeRecord(
        path="/media/Movie.mkv",
        node_id="file1",
        name="Movie.mkv",
        part_idx=0,
        channel_id=-100,
        message_id=101,
        doc_id_db=1001,
        doc_id_tg=1001,
        size_db=10,
        size_tg=10,
        caption_expected="fileName: Movie.mkv",
        caption_changed=True,
        status="dry_run",
    )
    module._write_jsonl_report(module.SanitizeReport(records=[record]), report_path)
    stale_part = SimpleNamespace(
        file_id="file1",
        idx=0,
        channel_id=-100,
        message_id=999,
        doc_id=1001,
        size=10,
        original_filename="Old physical.mkv",
    )

    class FakeRepo:
        async def parts_of(self, file_id):
            return [stale_part]

        async def set_part_original_filename(self, file_id, idx, original_filename):
            raise AssertionError("must not update stale rows")

    class FakeGateway:
        async def edit_message_caption(self, channel_id, message_id, caption):
            raise AssertionError("must not edit stale rows")

    result = await module.apply_report(
        FakeRepo(), FakeGateway(), report_path, progress_every=1
    )

    assert result.caption_updates == 0
    assert result.original_filename_updates == 0
    assert result.errors == 1
    assert result.records[0].status == "error"
    assert result.records[0].error == "stale_report"


def test_sanitize_cli_apply_report_uses_report_runner(tmp_path, monkeypatch):
    module = load_sanitize_captions_module()
    monkeypatch.delenv("DB", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
db: postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf
telegram:
  upload:
    channel: -100123
""",
        encoding="utf-8",
    )
    report_path = tmp_path / "dry-run.jsonl"
    report_path.write_text("", encoding="utf-8")
    calls = {}

    async def fake_run_apply_report(
        db_dsn,
        source_report,
        *,
        config_path,
        runtime_config,
        progress_every,
    ):
        calls["db_dsn"] = db_dsn
        calls["source_report"] = source_report
        calls["config_path"] = config_path
        calls["runtime_config"] = runtime_config
        calls["progress_every"] = progress_every
        return module.SanitizeReport(checked=1, parts=1, caption_updates=1)

    monkeypatch.setattr(module, "run_apply_report", fake_run_apply_report)

    rc = module.main([
        "--apply-report", str(report_path),
        "--config", str(config_path),
        "--progress-every", "7",
    ])

    assert rc == 0
    assert calls["db_dsn"] == "postgresql+asyncpg://cfg-user:cfg-pass@127.0.0.1/tgshelf"
    assert calls["source_report"] == str(report_path)
    assert calls["config_path"] == str(config_path)
    assert calls["progress_every"] == 7


@pytest.mark.asyncio
async def test_sanitize_logs_scan_and_telegram_progress(caplog):
    module = load_sanitize_captions_module()
    root = SimpleNamespace(id="root", name="media", is_folder=True, state="ACTIVE")
    file_node = SimpleNamespace(
        id="file1", name="Movie.mkv", is_folder=False, state="ACTIVE", size=10
    )
    part = SimpleNamespace(
        file_id="file1",
        idx=0,
        channel_id=-100,
        message_id=101,
        doc_id=1001,
        size=10,
        original_filename="Movie physical.mkv",
    )

    class FakeSession:
        async def commit(self):
            pass

    class FakeRepo:
        session = FakeSession()

        async def children(self, node_id, state="ACTIVE"):
            return [file_node] if node_id == "root" else []

        async def content_of(self, node_id):
            return None

        async def parts_of(self, node_id):
            return [part]

        async def path_of(self, node_id):
            return "/media/Movie.mkv"

        async def set_part_original_filename(self, file_id, idx, original_filename):
            pass

    class FakeGateway:
        async def get_document(self, channel_id, message_id):
            return SimpleNamespace(
                doc_id=1001,
                size=10,
                filename="Movie physical.mkv",
                caption="fileName: Old.mkv",
            )

        async def edit_message_caption(self, channel_id, message_id, caption):
            pass

    caplog.set_level(logging.INFO, logger="tgshelf.sanitize")

    report = await module.sanitize(
        FakeRepo(),
        root,
        FakeGateway(),
        dry_run=False,
        progress_every=1,
    )

    assert report.caption_updates == 1
    messages = [record.getMessage() for record in caplog.records]
    assert "[sanitize] walking ACTIVE files under 'media' depth=0 dry_run=False" in messages
    assert "[sanitize] db scan progress: 1 file(s), 1 telegram part(s)" in messages
    assert "[sanitize] db scan complete: 1 file(s), 1 telegram part(s)" in messages
    assert "[sanitize] telegram progress: 1/1 part(s), 100% completed" in messages
    assert "[sanitize] complete: 1 file(s), 1 part(s), 1 caption update(s), 0 original filename update(s), 0 error(s)" in messages


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
                    caption="fileName: Wrong Name.mkv.002",
                )
            return SimpleNamespace(
                doc_id=9999,
                size=9,
                caption="fileName: Movie Renamed.mkv.003",
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
    assert "caption_expected: fileName: Movie Renamed.mkv.002" in out
    assert "caption_actual: fileName: Wrong Name.mkv.002" in out
    assert "doc_id_db: 1003" in out
    assert "doc_id_tg: 9999" in out
    assert "size_db: 10" in out
    assert "size_tg: 9" in out

    rows = [json.loads(line) for line in report_path.read_text().splitlines()]
    assert rows[0]["issue"] == "missing_message"
    assert rows[0]["path"] == "/media/Movie Renamed.mkv"
    assert rows[0]["part_idx"] == 1
    assert rows[0]["clients_tried"] == ["user_main"]
    assert rows[1]["caption_expected"] == "fileName: Movie Renamed.mkv.002"


@pytest.mark.asyncio
async def test_deep_telegram_accepts_filename_line_in_multiline_caption():
    module = load_check_integrity_module()
    part = SimpleNamespace(
        idx=1,
        channel_id=-100,
        message_id=101,
        doc_id=1001,
        size=10,
        original_filename="Inception (2010) - WebDL 1080p AC3 10.1 GB.mkv.001",
    )

    class FakeGateway:
        async def get_document(self, channel_id, message_id):
            return SimpleNamespace(
                doc_id=1001,
                size=10,
                caption=(
                    "fileName: Inception (2010) - WebDL 1080p AC3 10.1 GB.mkv.001\n"
                    "season: 0\n"
                    "part: 1/3"
                ),
            )

    issues = await module.verify_telegram_part(
        [module.TelegramProbe("user_main", FakeGateway())],
        part,
        deep=True,
    )

    assert issues == []


@pytest.mark.asyncio
async def test_deep_telegram_accepts_logical_filename_for_legacy_part_names():
    module = load_check_integrity_module()
    node = SimpleNamespace(
        id="file1",
        name="I Griffin presentano - It's a Trap! (2010) - 1080p h264 AAC 2ch 2,80 G.mkv",
        size=2802423554,
    )
    parts = [
        SimpleNamespace(
            idx=0,
            channel_id=-1001609217041,
            message_id=1621,
            doc_id=5983334685407713011,
            size=2802423554,
            original_filename="I_Griffin_presentano_It's_a_Trap!_2010_1080p_h264_AAC_2ch_2,80_G.mkv",
        )
    ]

    class FakeRepo:
        async def content_of(self, node_id):
            return None

        async def parts_of(self, node_id):
            return parts

        async def path_of(self, node_id):
            return "/media/animation/I Griffin presentano - It's a Trap! (2010)/" + node.name

    class FakeGateway:
        async def get_document(self, channel_id, message_id):
            return SimpleNamespace(
                doc_id=5983334685407713011,
                size=2802423554,
                caption="fileName: I Griffin presentano - It's a Trap! (2010) - 1080p h264 AAC 2ch 2,80 G.mkv",
            )

    verdict = await module.check_file(
        FakeRepo(),
        node,
        probes=[module.TelegramProbe("user_main", FakeGateway())],
        deep_telegram=True,
    )

    assert verdict.issues == []


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
                caption=f"fileName: file.{message_id:03d}",
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
                caption=f"fileName: {'one' if message_id == 1 else 'two'}.mkv",
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
                caption=f"fileName: {'one' if message_id == 1 else 'two'}.mkv",
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
async def test_check_skips_folders_that_only_repeat_inherited_channel(caplog):
    module = load_check_integrity_module()
    root = SimpleNamespace(id="root", name="media", is_folder=True, size=0, channel_id=None)
    movies = SimpleNamespace(id="movies", name="movies", is_folder=True, size=0, channel_id=-1001)
    inherited = SimpleNamespace(id="inherited", name="Inception (2010)", is_folder=True, size=0, channel_id=-1001)
    file_node = SimpleNamespace(id="file1", name="movie.mkv", is_folder=False, size=3, channel_id=-1001)

    class FakeRepo:
        async def children(self, node_id):
            return {
                "root": [movies],
                "movies": [inherited],
                "inherited": [file_node],
            }.get(node_id, [])

        async def content_of(self, node_id):
            return b"abc"

        async def parts_of(self, node_id):
            return []

        async def path_of(self, node_id):
            return {
                "root": "/media",
                "movies": "/media/movies",
                "inherited": "/media/movies/Inception (2010)",
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
    assert not any("processing channel folder: /media/movies/Inception (2010)" in message for message in messages)


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
                caption="fileName: anim.mkv" if message_id == 11 else "fileName: movie.mkv",
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
