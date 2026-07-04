from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def load_recovery_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "recover_missing_from_backup.py"
    spec = importlib.util.spec_from_file_location("recover_missing_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def recovery_record(**overrides):
    record = {
        "status": "backup_verified",
        "media": {
            "path": "/media/tvshows/Show/file.mkv",
            "node_id": "media1",
            "name": "Show - S01E01.mkv",
            "expected_main_part": {
                "part_idx": 0,
                "channel_id": -1001,
                "message_id": 10,
                "doc_id_db": 42,
                "size_db": 123,
                "original_filename_db": "Show_S01E01.mkv",
            },
        },
        "backup": {
            "path": "/backup/tvshows-bk-1/Show/file.mkv",
            "node_id": "backup1",
            "selected_part": {
                "idx": 0,
                "channel_id": -2001,
                "message_id": 20,
                "doc_id": 42,
                "size": 123,
                "original_filename": "Show_S01E01.mkv",
            },
        },
        "restore": {"main_channel_id": -1001},
        "checks": [
            {
                "name": "backup_telegram_deep_check",
                "status": "ok",
                "issues": [],
            },
            {
                "name": "backup_part_matches_missing_media_part",
                "status": "ok",
                "size_matches": True,
                "doc_id_matches": True,
                "original_filename_matches": True,
            },
        ],
    }
    record.update(overrides)
    return record


def test_load_recovery_plan_accepts_verified_records_and_caption_warnings(tmp_path):
    module = load_recovery_module()
    plan = tmp_path / "plan.jsonl"
    good = recovery_record()
    warning = recovery_record(
        status="backup_verified_with_warning",
        checks=[
            {
                "name": "backup_telegram_deep_check",
                "status": "warning",
                "issues": [{"code": "caption_mismatch"}],
            },
            {
                "name": "backup_part_matches_missing_media_part",
                "status": "ok",
                "size_matches": True,
                "doc_id_matches": True,
                "original_filename_matches": True,
            },
        ],
    )
    bad = recovery_record(status="backup_path_not_found")
    plan.write_text(
        "\n".join(json.dumps(row) for row in (good, warning, bad)) + "\n",
        encoding="utf-8",
    )

    entries = module.load_recovery_plan(plan)

    assert [entry.media_path for entry in entries] == [
        "/media/tvshows/Show/file.mkv",
        "/media/tvshows/Show/file.mkv",
    ]


def test_canonical_caption_uses_part_original_filename():
    module = load_recovery_module()
    entry = module.RecoveryEntry.from_record(recovery_record())

    assert module.canonical_caption(entry) == "fileName: Show_S01E01.mkv"


def test_canonical_caption_preserves_existing_part_suffix():
    module = load_recovery_module()
    record = recovery_record()
    record["media"]["name"] = "Movie.mkv"
    record["backup"]["selected_part"]["idx"] = 1
    record["backup"]["selected_part"]["original_filename"] = "Movie.mkv.002"
    entry = module.RecoveryEntry.from_record(record)

    assert module.canonical_caption(entry) == "fileName: Movie.mkv.002"


def test_dry_run_is_explicit_and_apply_is_implicit():
    module = load_recovery_module()

    dry = module.parse_args(["--dry-run"])
    apply = module.parse_args([])

    assert dry.dry_run is True
    assert apply.dry_run is False


@pytest.mark.asyncio
async def test_copy_entry_with_cooldown_retry_sleeps_and_retries(monkeypatch):
    module = load_recovery_module()
    entry = module.RecoveryEntry.from_record(recovery_record())
    expected = entry.backup_part
    attempts = 0
    sleeps = []

    async def fake_copy_entry(gateway, entry_arg, caption):
        nonlocal attempts
        attempts += 1
        assert gateway == "gateway"
        assert entry_arg is entry
        assert caption == "fileName: Show_S01E01.mkv"
        if attempts == 1:
            raise module.FloodCooldown(3)
        return expected

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(module, "_copy_entry", fake_copy_entry)

    result = await module.copy_entry_with_cooldown_retry(
        "gateway",
        entry,
        "fileName: Show_S01E01.mkv",
        sleep=fake_sleep,
    )

    assert result is expected
    assert attempts == 2
    assert sleeps == [3]
