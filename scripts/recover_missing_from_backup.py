"""Restore missing main-channel messages from verified backup-channel parts.

This is an operational migration tool, not a public `tgshelf` subcommand. It
consumes the JSONL recovery plan produced after `scripts/check_integrity.py`,
copies each verified backup message back into the expected main channel with a
canonical caption, verifies the new Telegram message, then updates the matching
DB part row.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import update

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgshelf.config import load_config  # noqa: E402
from tgshelf.core import channels  # noqa: E402
from tgshelf.core.upload import PartRecord  # noqa: E402
from tgshelf.db.engine import create_engine, create_session_factory  # noqa: E402
from tgshelf.db.models import Part  # noqa: E402
from tgshelf.http.serve import make_rate_limiter, start_clients  # noqa: E402
from tgshelf.log import setup_logging  # noqa: E402

log = logging.getLogger("tgshelf.recover")

DEFAULT_PLAN = Path("/mnt/shared/Personal/logs/tgshelf-missing-recovery-plan.jsonl")
DEFAULT_LEDGER = Path("/mnt/shared/Personal/logs/tgshelf-missing-recovery-ledger.jsonl")
ACCEPTED_WARNING_CODES = {"caption_mismatch"}


@dataclass(frozen=True)
class RecoveryEntry:
    key: str
    media_path: str
    media_node_id: str
    media_name: str
    part_idx: int
    main_channel_id: int
    old_message_id: int
    expected_doc_id: int | None
    expected_size: int
    expected_original_filename: str | None
    backup_path: str
    backup_part: PartRecord

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "RecoveryEntry":
        media = record["media"]
        expected = media["expected_main_part"]
        backup = record["backup"]
        selected = backup["selected_part"]
        media_path = media["path"]
        part_idx = int(expected["part_idx"])
        return cls(
            key=f"{media['node_id']}:{part_idx}",
            media_path=media_path,
            media_node_id=media["node_id"],
            media_name=media["name"],
            part_idx=part_idx,
            main_channel_id=int(record["restore"]["main_channel_id"]),
            old_message_id=int(expected["message_id"]),
            expected_doc_id=expected.get("doc_id_db"),
            expected_size=int(expected["size_db"]),
            expected_original_filename=expected.get("original_filename_db"),
            backup_path=backup["path"],
            backup_part=PartRecord(
                idx=int(selected["idx"]),
                channel_id=int(selected["channel_id"]),
                message_id=int(selected["message_id"]),
                doc_id=selected.get("doc_id"),
                size=int(selected["size"]),
                original_filename=selected.get("original_filename"),
            ),
        )


@dataclass(frozen=True)
class LedgerEvent:
    key: str
    state: str
    media_path: str
    new_part: dict[str, Any] | None = None
    caption: str | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="recover_missing_from_backup.py",
        description="Restore verified missing main-channel messages from backup channels.",
    )
    parser.add_argument("--config", default="./config.yaml", help="path to config.yaml")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN, help="recovery plan JSONL")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="resumable ledger JSONL")
    parser.add_argument("--dry-run", action="store_true", help="simulate only; absence applies changes")
    parser.add_argument("--limit", type=int, default=0, help="process only the first N entries")
    return parser.parse_args(argv)


def _issue_codes(record: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for check in record.get("checks", []):
        for issue in check.get("issues", []):
            code = issue.get("code")
            if code:
                codes.add(str(code))
    return codes


def _strong_part_match(record: dict[str, Any]) -> bool:
    for check in record.get("checks", []):
        if check.get("name") == "backup_part_matches_missing_media_part":
            return (
                check.get("size_matches") is True
                and check.get("doc_id_matches") is True
                and check.get("original_filename_matches") is True
            )
    return False


def _is_recoverable_record(record: dict[str, Any]) -> bool:
    status = record.get("status")
    if not _strong_part_match(record):
        return False
    if status == "backup_verified":
        return True
    if status == "backup_verified_with_warning":
        return _issue_codes(record).issubset(ACCEPTED_WARNING_CODES)
    return False


def load_recovery_plan(path: Path) -> list[RecoveryEntry]:
    entries: list[RecoveryEntry] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            record = json.loads(line)
            if _is_recoverable_record(record):
                entries.append(RecoveryEntry.from_record(record))
    return entries


def canonical_caption(entry: RecoveryEntry) -> str:
    filename = entry.backup_part.original_filename or entry.expected_original_filename
    if not filename:
        raise RuntimeError(f"missing original filename for {entry.media_path}")
    return f"fileName: {filename}"


def _caption_filename(caption: str | None) -> str | None:
    if not isinstance(caption, str):
        return None
    for line in caption.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "filename":
            return value.strip() or None
    return None


def _part_to_dict(part: PartRecord) -> dict[str, Any]:
    return asdict(part)


def _part_from_dict(value: dict[str, Any]) -> PartRecord:
    return PartRecord(
        idx=int(value["idx"]),
        channel_id=int(value["channel_id"]),
        message_id=int(value["message_id"]),
        doc_id=value.get("doc_id"),
        size=int(value["size"]),
        original_filename=value.get("original_filename"),
    )


def load_ledger(path: Path) -> dict[str, LedgerEvent]:
    if not path.exists():
        return {}
    latest: dict[str, LedgerEvent] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            latest[row["key"]] = LedgerEvent(
                key=row["key"],
                state=row["state"],
                media_path=row["media_path"],
                new_part=row.get("new_part"),
                caption=row.get("caption"),
            )
    return latest


def append_ledger(path: Path, event: LedgerEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(event), ensure_ascii=False, sort_keys=True) + "\n")


async def verify_new_part(gateway: Any, part: PartRecord, caption: str) -> None:
    ref = await gateway.get_document(part.channel_id, part.message_id)
    if ref is None:
        raise RuntimeError(
            f"new message {part.channel_id}/{part.message_id} is missing after copy"
        )
    if part.doc_id is not None and ref.doc_id is not None and ref.doc_id != part.doc_id:
        raise RuntimeError(
            f"new message doc_id mismatch for {part.channel_id}/{part.message_id}: "
            f"expected {part.doc_id}, got {ref.doc_id}"
        )
    if ref.size != part.size:
        raise RuntimeError(
            f"new message size mismatch for {part.channel_id}/{part.message_id}: "
            f"expected {part.size}, got {ref.size}"
        )
    expected_filename = _caption_filename(caption)
    actual_filename = _caption_filename(getattr(ref, "caption", None))
    if actual_filename != expected_filename:
        raise RuntimeError(
            f"new message caption mismatch for {part.channel_id}/{part.message_id}: "
            f"expected {expected_filename!r}, got {actual_filename!r}"
        )


async def update_media_part(session, entry: RecoveryEntry, new_part: PartRecord) -> None:
    result = await session.execute(
        update(Part)
        .where(
            Part.file_id == entry.media_node_id,
            Part.idx == entry.part_idx,
            Part.channel_id == entry.main_channel_id,
            Part.message_id == entry.old_message_id,
            Part.doc_id == entry.expected_doc_id,
            Part.size == entry.expected_size,
            Part.original_filename == entry.expected_original_filename,
        )
        .values(
            channel_id=new_part.channel_id,
            message_id=new_part.message_id,
            doc_id=new_part.doc_id,
            size=new_part.size,
            original_filename=new_part.original_filename,
        )
    )
    if result.rowcount != 1:
        await session.rollback()
        raise RuntimeError(
            f"DB part guard matched {result.rowcount} rows for {entry.media_path} part {entry.part_idx}"
        )
    await session.commit()


async def _copy_entry(gateway: Any, entry: RecoveryEntry, caption: str) -> PartRecord:
    copied = await channels.forward_parts(
        gateway,
        [entry.backup_part],
        entry.main_channel_id,
        always_copy=True,
        caption_factory=lambda _part: caption,
    )
    return copied[0]


async def apply_recovery(config_path: str, entries: list[RecoveryEntry], ledger_path: Path) -> int:
    config = load_config(config_path)
    setup_logging(config.logger)
    engine = create_engine(config.db)
    session_factory = create_session_factory(engine)
    rate_limiter = make_rate_limiter(config.telegram.rate_limit)
    clients = await start_clients(config, rate_limiter)
    user_clients = [(account, client) for account, client in clients if not account.is_bot]
    if not user_clients:
        raise RuntimeError("no authorized user account available for Telegram writes")
    account, gateway = user_clients[0]
    log.info("[recover] using account '%s' for Telegram writes", account.name)

    latest = load_ledger(ledger_path)
    updated = 0
    try:
        async with session_factory() as session:
            for entry in entries:
                caption = canonical_caption(entry)
                previous = latest.get(entry.key)
                if previous is not None and previous.state == "db_updated":
                    log.info("[recover] skipping already updated %s", entry.media_path)
                    continue
                if previous is not None and previous.new_part:
                    new_part = _part_from_dict(previous.new_part)
                    log.info("[recover] resuming copied message for %s", entry.media_path)
                else:
                    log.info(
                        "[recover] copying backup %s/%s -> main %s for %s",
                        entry.backup_part.channel_id,
                        entry.backup_part.message_id,
                        entry.main_channel_id,
                        entry.media_path,
                    )
                    new_part = await _copy_entry(gateway, entry, caption)
                    append_ledger(
                        ledger_path,
                        LedgerEvent(
                            key=entry.key,
                            state="copied_to_main",
                            media_path=entry.media_path,
                            new_part=_part_to_dict(new_part),
                            caption=caption,
                        ),
                    )
                await verify_new_part(gateway, new_part, caption)
                append_ledger(
                    ledger_path,
                    LedgerEvent(
                        key=entry.key,
                        state="verified_main",
                        media_path=entry.media_path,
                        new_part=_part_to_dict(new_part),
                        caption=caption,
                    ),
                )
                await update_media_part(session, entry, new_part)
                append_ledger(
                    ledger_path,
                    LedgerEvent(
                        key=entry.key,
                        state="db_updated",
                        media_path=entry.media_path,
                        new_part=_part_to_dict(new_part),
                        caption=caption,
                    ),
                )
                updated += 1
                log.info("[recover] restored %s", entry.media_path)
    finally:
        for _account, client in clients:
            try:
                await client._client.disconnect()
            except Exception:
                pass
        await engine.dispose()
    return updated


def dry_run(entries: list[RecoveryEntry]) -> None:
    print(f"dry-run: {len(entries)} recoverable part(s)")
    for entry in entries:
        print(
            f"would restore {entry.media_path} part {entry.part_idx}: "
            f"{entry.backup_part.channel_id}/{entry.backup_part.message_id} -> "
            f"{entry.main_channel_id} caption={canonical_caption(entry)!r}"
        )


async def async_main(args: argparse.Namespace) -> int:
    entries = load_recovery_plan(args.plan)
    if args.limit > 0:
        entries = entries[: args.limit]
    if args.dry_run:
        dry_run(entries)
        return 0
    updated = await apply_recovery(args.config, entries, args.ledger)
    print(f"restored {updated} part(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
