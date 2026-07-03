"""Sanitize Telegram captions and immutable physical filenames for tgshelf.

This maintenance script walks ACTIVE Telegram-backed files and aligns:

- Telegram message captions with the current logical node name.
- `parts.original_filename` with the physical filename reported by Telegram.

Default mode applies changes. Pass `--dry-run` to report without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tgshelf.core.captions import caption_first_line_filename, logical_part_caption

log = logging.getLogger("tgshelf.sanitize")


@dataclass
class SanitizeRecord:
    path: str
    node_id: str
    name: str
    part_idx: int
    channel_id: int
    message_id: int
    doc_id_db: int | None
    doc_id_tg: int | None = None
    size_db: int | None = None
    size_tg: int | None = None
    caption_expected: str | None = None
    caption_actual: str | None = None
    original_filename_db: str | None = None
    original_filename_tg: str | None = None
    caption_changed: bool = False
    original_filename_changed: bool = False
    status: str = "ok"
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SanitizeReport:
    checked: int = 0
    parts: int = 0
    caption_updates: int = 0
    original_filename_updates: int = 0
    errors: int = 0
    records: list[SanitizeRecord] = field(default_factory=list)

    def extend(self, other: "SanitizeReport") -> None:
        self.checked += other.checked
        self.parts += other.parts
        self.caption_updates += other.caption_updates
        self.original_filename_updates += other.original_filename_updates
        self.errors += other.errors
        self.records.extend(other.records)

    def __str__(self) -> str:
        return (
            f"{self.checked} file(s), {self.parts} part(s), "
            f"{self.caption_updates} caption update(s), "
            f"{self.original_filename_updates} original filename update(s), "
            f"{self.errors} error(s)"
        )


def _caption_expected(name: str, *, position: int, total_parts: int) -> str:
    return logical_part_caption(name, idx=position, total_parts=total_parts)


async def sanitize_file(repo, node, gateway, *, dry_run: bool) -> SanitizeReport:
    content = await repo.content_of(node.id)
    if content is not None:
        return SanitizeReport()
    parts = list(await repo.parts_of(node.id))
    if not parts:
        return SanitizeReport()
    path = await repo.path_of(node.id) or node.name
    return await _sanitize_file_parts(
        repo, node, parts, path, gateway, dry_run=dry_run
    )


async def _sanitize_file_parts(
    repo, node, parts: list[Any], path: str, gateway, *, dry_run: bool
) -> SanitizeReport:
    report = SanitizeReport(checked=1, parts=len(parts))
    total_parts = len(parts)

    for position, part in enumerate(parts):
        expected_caption = _caption_expected(
            node.name, position=position, total_parts=total_parts
        )
        record = SanitizeRecord(
            path=path,
            node_id=node.id,
            name=node.name,
            part_idx=part.idx,
            channel_id=part.channel_id,
            message_id=part.message_id,
            doc_id_db=part.doc_id,
            size_db=part.size,
            caption_expected=expected_caption,
            original_filename_db=part.original_filename,
        )

        try:
            ref = await gateway.get_document(part.channel_id, part.message_id)
            if ref is None:
                _record_error(report, record, "missing_message")
                continue
            record.doc_id_tg = ref.doc_id
            record.size_tg = ref.size
            record.caption_actual = getattr(ref, "caption", None)
            record.original_filename_tg = ref.filename

            if part.doc_id is not None and ref.doc_id is not None and ref.doc_id != part.doc_id:
                _record_error(report, record, "doc_id_mismatch")
                continue
            if ref.size != part.size:
                _record_error(report, record, "part_size_mismatch")
                continue

            actual_filename = caption_first_line_filename(record.caption_actual)
            expected_filename = expected_caption.removeprefix("fileName: ")
            caption_changed = actual_filename != expected_filename
            original_filename_changed = (
                bool(ref.filename) and ref.filename != part.original_filename
            )
            if not caption_changed and not original_filename_changed:
                continue

            record.caption_changed = caption_changed
            record.original_filename_changed = original_filename_changed
            if dry_run:
                record.status = "dry_run"
                report.records.append(record)
                if caption_changed:
                    report.caption_updates += 1
                if original_filename_changed:
                    report.original_filename_updates += 1
                continue

            if caption_changed:
                await gateway.edit_message_caption(
                    part.channel_id, part.message_id, expected_caption
                )
            if original_filename_changed:
                await repo.set_part_original_filename(
                    part.file_id, part.idx, ref.filename
                )
            record.status = "updated"
            report.records.append(record)
            if caption_changed:
                report.caption_updates += 1
            if original_filename_changed:
                report.original_filename_updates += 1
        except Exception as exc:  # noqa: BLE001 - keep sanitizing other parts
            record.details["exception"] = str(exc)
            _record_error(report, record, exc.__class__.__name__)

    return report


def _record_error(report: SanitizeReport, record: SanitizeRecord, code: str) -> None:
    record.status = "error"
    record.error = code
    report.errors += 1
    report.records.append(record)


async def walk_active_files(repo, start_node, depth: int) -> AsyncIterator[Any]:
    if start_node.state != "ACTIVE":
        return
    if not start_node.is_folder:
        yield start_node
        return
    queue = [(start_node, 1)]
    while queue:
        folder, level = queue.pop(0)
        for child in await repo.children(folder.id, state="ACTIVE"):
            if child.is_folder:
                if depth == 0 or level < depth:
                    queue.append((child, level + 1))
            else:
                yield child


async def sanitize(
    repo,
    start_node,
    gateway,
    *,
    depth: int = 0,
    dry_run: bool = False,
    concurrent: int = 1,
    progress_every: int = 100,
) -> SanitizeReport:
    progress_every = max(1, progress_every)
    files: list[tuple[Any, list[Any], str]] = []
    log.info(
        "[sanitize] walking ACTIVE files under '%s' depth=%s dry_run=%s",
        start_node.name,
        depth,
        dry_run,
    )
    async for node in walk_active_files(repo, start_node, depth):
        content = await repo.content_of(node.id)
        if content is not None:
            continue
        parts = list(await repo.parts_of(node.id))
        if not parts:
            continue
        path = await repo.path_of(node.id) or node.name
        files.append((node, parts, path))
        if len(files) % progress_every == 0:
            log.info(
                "[sanitize] db scan progress: %s file(s), %s telegram part(s)",
                len(files),
                sum(len(item[1]) for item in files),
            )

    total_parts = sum(len(parts) for _node, parts, _path in files)
    log.info(
        "[sanitize] db scan complete: %s file(s), %s telegram part(s)",
        len(files),
        total_parts,
    )

    report = SanitizeReport()
    completed_parts = 0
    async def run_one(item):
        nonlocal completed_parts
        node, parts, path = item
        result = await _sanitize_file_parts(
            repo, node, parts, path, gateway, dry_run=dry_run
        )
        if not dry_run and result.original_filename_updates:
            await repo.session.commit()
        completed_parts += result.parts
        if completed_parts % progress_every == 0 or completed_parts == total_parts:
            log.info(
                "[sanitize] telegram progress: %s/%s part(s)",
                completed_parts,
                total_parts,
            )
        return result

    if concurrent > 1:
        log.info("[sanitize] processing sequentially to keep DB writes session-safe")
    for item in files:
        partial = await run_one(item)
        report.extend(partial)
    log.info("[sanitize] complete: %s", report)
    return report


def _print_report(report: SanitizeReport) -> None:
    print(f"\nsanitize report: {report}")
    for record in report.records:
        print(f"\n{record.path}")
        print(f"  node_id: {record.node_id}")
        print(f"  part: {record.part_idx}")
        print(f"  status: {record.status}")
        if record.error:
            print(f"  error: {record.error}")
        if record.caption_changed or record.error:
            print(f"  caption_expected: {record.caption_expected}")
            print(f"  caption_actual: {record.caption_actual}")
        if record.original_filename_changed or record.error:
            print(f"  original_filename_db: {record.original_filename_db}")
            print(f"  original_filename_tg: {record.original_filename_tg}")


def _write_jsonl_report(report: SanitizeReport, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in report.records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def _read_jsonl_report(path: str | Path) -> list[SanitizeRecord]:
    records: list[SanitizeRecord] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError(f"invalid JSONL record at line {line_no}")
            data.setdefault("details", {})
            records.append(SanitizeRecord(**data))
    return records


def _record_is_actionable(record: SanitizeRecord) -> bool:
    return (
        record.status == "dry_run"
        and (record.caption_changed or record.original_filename_changed)
    )


async def _matching_report_part(repo, record: SanitizeRecord):
    for part in await repo.parts_of(record.node_id):
        if part.idx == record.part_idx:
            break
    else:
        return None

    checks = [
        part.channel_id == record.channel_id,
        part.message_id == record.message_id,
    ]
    if record.doc_id_db is not None:
        checks.append(part.doc_id == record.doc_id_db)
    if record.doc_id_tg is not None:
        checks.append(part.doc_id == record.doc_id_tg)
    if record.size_db is not None:
        checks.append(part.size == record.size_db)
    if record.size_tg is not None:
        checks.append(part.size == record.size_tg)
    return part if all(checks) else None


async def apply_report(
    repo,
    gateway,
    source_report: str | Path,
    *,
    progress_every: int = 100,
) -> SanitizeReport:
    progress_every = max(1, progress_every)
    source_records = _read_jsonl_report(source_report)
    actionable = [record for record in source_records if _record_is_actionable(record)]
    report = SanitizeReport(
        checked=len({record.node_id for record in actionable}),
        parts=len(actionable),
    )
    log.info(
        "[sanitize] applying report=%s actionable=%s",
        source_report,
        len(actionable),
    )

    for position, source in enumerate(actionable, start=1):
        record = replace(source, status="updated", error=None, details=dict(source.details))
        try:
            part = await _matching_report_part(repo, record)
            if part is None:
                _record_error(report, replace(record), "stale_report")
                continue

            if record.caption_changed:
                if not record.caption_expected:
                    _record_error(report, replace(record), "missing_caption_expected")
                    continue
                await gateway.edit_message_caption(
                    record.channel_id, record.message_id, record.caption_expected
                )
                report.caption_updates += 1

            if record.original_filename_changed:
                if not record.original_filename_tg:
                    _record_error(report, replace(record), "missing_original_filename_tg")
                    continue
                await repo.set_part_original_filename(
                    part.file_id, part.idx, record.original_filename_tg
                )
                await repo.session.commit()
                report.original_filename_updates += 1

            report.records.append(record)
        except Exception as exc:  # noqa: BLE001 - keep applying other records
            record.details["exception"] = str(exc)
            _record_error(report, record, exc.__class__.__name__)

        if position % progress_every == 0 or position == len(actionable):
            log.info(
                "[sanitize] apply-report progress: %s/%s part(s)",
                position,
                len(actionable),
            )

    log.info("[sanitize] apply-report complete: %s", report)
    return report


async def run(
    db_dsn: str,
    path: str,
    *,
    depth: int,
    dry_run: bool,
    concurrent: int,
    config_path: str,
    runtime_config=None,
    progress_every: int = 100,
) -> SanitizeReport:
    from tgshelf.db.engine import create_engine, create_session_factory
    from tgshelf.db.repo import NodeRepo

    gateway, clients = await _build_gateway(config_path, runtime_config=runtime_config)
    engine = create_engine(db_dsn)
    try:
        async with create_session_factory(engine)() as session:
            repo = NodeRepo(session)
            start = await repo.resolve(path)
            if start is None:
                raise SystemExit(f"path not found: {path}")
            report = await sanitize(
                repo,
                start,
                gateway,
                depth=depth,
                dry_run=dry_run,
                concurrent=concurrent,
                progress_every=progress_every,
            )
    finally:
        await engine.dispose()
        for client in clients:
            disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
            if disconnect is not None:
                await disconnect()
    return report


async def run_apply_report(
    db_dsn: str,
    source_report: str,
    *,
    config_path: str,
    runtime_config=None,
    progress_every: int = 100,
) -> SanitizeReport:
    from tgshelf.db.engine import create_engine, create_session_factory
    from tgshelf.db.repo import NodeRepo

    gateway, clients = await _build_gateway(config_path, runtime_config=runtime_config)
    engine = create_engine(db_dsn)
    try:
        async with create_session_factory(engine)() as session:
            repo = NodeRepo(session)
            report = await apply_report(
                repo,
                gateway,
                source_report,
                progress_every=progress_every,
            )
    finally:
        await engine.dispose()
        for client in clients:
            disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
            if disconnect is not None:
                await disconnect()
    return report


async def _build_gateway(config_path: str, *, runtime_config=None):
    from tgshelf.config import load_config
    from tgshelf.http.serve import make_rate_limiter, start_clients

    config = runtime_config or load_config(config_path)
    rl = make_rate_limiter(config.telegram.rate_limit)
    pairs = await start_clients(config, rl)
    clients = [client for _account, client in pairs]
    selected = [(account, client) for account, client in pairs if not account.is_bot]
    if not selected:
        raise SystemExit("no usable user account; run `tgshelf accounts login <name>`")
    account, gateway = selected[0]
    log.info(
        "[sanitize] telegram gateway ready: %s selected from %s connected account(s)",
        account.name,
        len(pairs),
    )
    return gateway, clients


def _load_runtime_config(config_path: str):
    from tgshelf.config import load_config

    return load_config(config_path)


def main(argv: list[str] | None = None) -> int:
    from tgshelf.commands.common import resolve_concurrent

    parser = argparse.ArgumentParser(description="Sanitize tgshelf Telegram captions.")
    parser.add_argument("path", nargs="?", default="/",
                        help="path of an ACTIVE folder or file to sanitize")
    parser.add_argument("--db", default=os.environ.get("DB"),
                        help="Postgres DSN override (default: env DB, then config db)")
    parser.add_argument("--depth", type=int, default=0,
                        help="folder navigation depth from the start folder; 0 = infinite")
    parser.add_argument("--dry-run", action="store_true",
                        help="report planned changes without writing Telegram or DB")
    parser.add_argument("--apply-report",
                        help="apply an existing dry-run JSONL report without rescanning Telegram")
    parser.add_argument("--report-jsonl", help="write a machine-readable report as JSONL")
    parser.add_argument("--concurrent", type=int,
                        help="parallel file sanitizers (default: env CONCURRENCY, then config)")
    parser.add_argument("--progress-every", type=int, default=100,
                        help="log progress every N files/Telegram parts (default: 100)")
    parser.add_argument("--config", default="./config.yaml",
                        help="runtime config for db fallback and Telegram clients")
    parser.add_argument("--log", default="info", help="log level")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    runtime_config = _load_runtime_config(args.config)
    db_dsn = args.db or runtime_config.db
    if not db_dsn:
        parser.error("--db (or env DB, or config db) is required")
    if args.depth < 0:
        parser.error("--depth must be >= 0 (0 = infinite)")
    if args.progress_every < 1:
        parser.error("--progress-every must be >= 1")
    if args.apply_report and args.dry_run:
        parser.error("--dry-run cannot be used with --apply-report")
    try:
        concurrent = resolve_concurrent(runtime_config, cli_value=args.concurrent)
    except ValueError as exc:
        parser.error(str(exc))

    if args.apply_report:
        log.info("[sanitize] starting apply-report=%s", args.apply_report)
        report = asyncio.run(run_apply_report(
            db_dsn,
            args.apply_report,
            config_path=args.config,
            runtime_config=runtime_config,
            progress_every=args.progress_every,
        ))
    else:
        log.info(
            "[sanitize] starting path=%s dry_run=%s concurrency=%s",
            args.path,
            args.dry_run,
            concurrent,
        )
        report = asyncio.run(run(
            db_dsn,
            args.path,
            depth=args.depth,
            dry_run=args.dry_run,
            concurrent=concurrent,
            config_path=args.config,
            runtime_config=runtime_config,
            progress_every=args.progress_every,
        ))
    _print_report(report)
    if args.report_jsonl:
        _write_jsonl_report(report, args.report_jsonl)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
