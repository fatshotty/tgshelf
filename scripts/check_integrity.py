"""Standalone integrity check for tgshelf (NOT a `tgshelf` subcommand).

Two complementary levels (see docs/PLAN.md size-integrity section):

  - **DB-level** (default, cheap, no network): for every ACTIVE file, the expected
    size (`node.size`, frozen at upload/merge) must equal the effective size
    (`len(content)` inline, `sum(parts.size)` on Telegram). A mismatch means a lost
    or corrupted part row / denormalisation drift.
  - **Telegram-level** (`--verify-telegram`, heavier): each part's message must
    still exist and its doc_id/size must match. This is what catches *physically
    truncated* files — a deleted Telegram message leaves the part row (and its
    size) intact, so the DB-level check alone cannot see it.

The walk + per-file evaluation is independent of the Telegram client (a gateway
is injected) so it is testable with a fake; `main()` wires Postgres and, for
`--verify-telegram`, real clients from config.

Usage:
  python scripts/check_integrity.py /media --config ./config.yaml
  python scripts/check_integrity.py /media --verify-telegram --config ./config.yaml
  python scripts/check_integrity.py /media --db <DSN> --verify-telegram --config ./config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

log = logging.getLogger("tgshelf.check")


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    part_idx: int | None = None
    channel_id: int | None = None
    message_id: int | None = None
    doc_id_db: int | None = None
    doc_id_tg: int | None = None
    size_db: int | None = None
    size_tg: int | None = None
    original_filename_db: str | None = None
    caption_expected: str | None = None
    caption_actual: str | None = None
    verified_by: str | None = None
    clients_tried: list[str] = field(default_factory=list)
    recoverability: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verdict:
    node_id: str
    name: str
    path: str
    expected_size: int
    effective_size: int
    parts_count: int = 0
    issues: list[Issue] = field(default_factory=list)  # empty = OK

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


@dataclass
class Report:
    checked: int = 0
    bad: int = 0

    def __str__(self) -> str:
        return f"{self.checked} file(s) checked, {self.bad} with issues"


@dataclass(frozen=True)
class TelegramProbe:
    name: str
    gateway: Any


def _expected_caption(original_filename: str | None) -> str | None:
    if not original_filename:
        return None
    return f"filename: {original_filename}"


def _caption_filename(caption: str | None) -> str | None:
    if not isinstance(caption, str):
        return None
    for line in caption.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "filename":
            filename = value.strip()
            return filename or None
    return None


def _normalized_filename(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^0-9a-z]+", " ", value.replace("_", " ").casefold())
    return " ".join(normalized.split()) or None


def _caption_filename_candidates(part, logical_filename: str | None = None) -> list[str]:
    candidates: list[str] = []
    for candidate in (part.original_filename, logical_filename):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    if logical_filename and part.original_filename:
        suffix = re.search(r"(\.\d{3})$", part.original_filename)
        suffixed_logical = f"{logical_filename}{suffix.group(1)}" if suffix else None
        if suffixed_logical and suffixed_logical not in candidates:
            candidates.append(suffixed_logical)
    return candidates


def _caption_filename_matches(
    actual_filename: str | None, candidates: list[str]
) -> bool:
    if actual_filename is None:
        return False
    if actual_filename in candidates:
        return True
    normalized_actual = _normalized_filename(actual_filename)
    return any(
        normalized_actual == _normalized_filename(candidate)
        for candidate in candidates
    )


def _rotate_probes(probes: list[TelegramProbe], offset: int) -> list[TelegramProbe]:
    if not probes:
        return []
    pivot = offset % len(probes)
    return probes[pivot:] + probes[:pivot]


async def _get_document_from_any_probe(probes: list[TelegramProbe], part, *, offset: int = 0):
    clients_tried: list[str] = []
    client_errors: dict[str, str] = {}
    for probe in _rotate_probes(probes, offset):
        clients_tried.append(probe.name)
        try:
            ref = await probe.gateway.get_document(part.channel_id, part.message_id)
        except Exception as exc:  # noqa: BLE001 - keep trying other accounts/bots
            log.warning(
                "[check] telegram probe '%s' failed for channel_id=%s message_id=%s: %s",
                probe.name,
                part.channel_id,
                part.message_id,
                exc,
                exc_info=log.isEnabledFor(logging.DEBUG),
            )
            client_errors[probe.name] = str(exc)
            continue
        if ref is not None:
            return ref, probe.name, clients_tried, client_errors
    return None, None, clients_tried, client_errors


async def verify_telegram_part(
    probes: list[TelegramProbe], part, *, deep: bool, offset: int = 0,
    logical_filename: str | None = None,
) -> list[Issue]:
    """Per-part Telegram verification: message exists + doc_id/size/caption match."""
    issues: list[Issue] = []
    ref, verified_by, clients_tried, client_errors = await _get_document_from_any_probe(
        probes, part, offset=offset
    )
    if ref is None:
        code = "unreachable" if client_errors and len(client_errors) == len(clients_tried) else "missing_message"
        recoverability = (
            "cannot_confirm_message_state"
            if code == "unreachable"
            else "cannot_rebuild_part_from_telegram"
        )
        issues.append(Issue(
            code=code,
            severity="error",
            message=f"part {part.idx}: message {part.channel_id}/{part.message_id} {code}",
            part_idx=part.idx,
            channel_id=part.channel_id,
            message_id=part.message_id,
            doc_id_db=part.doc_id,
            size_db=part.size,
            original_filename_db=part.original_filename,
            caption_expected=_expected_caption(part.original_filename),
            clients_tried=clients_tried,
            recoverability=recoverability,
            details={"client_errors": client_errors} if client_errors else {},
        ))
        return issues
    if part.doc_id is not None and ref.doc_id is not None and ref.doc_id != part.doc_id:
        issues.append(Issue(
            code="doc_id_mismatch",
            severity="error",
            message=f"part {part.idx}: doc_id mismatch (db {part.doc_id} != tg {ref.doc_id})",
            part_idx=part.idx,
            channel_id=part.channel_id,
            message_id=part.message_id,
            doc_id_db=part.doc_id,
            doc_id_tg=ref.doc_id,
            size_db=part.size,
            size_tg=ref.size,
            original_filename_db=part.original_filename,
            caption_expected=_expected_caption(part.original_filename),
            caption_actual=getattr(ref, "caption", None),
            verified_by=verified_by,
            clients_tried=clients_tried,
            recoverability="message_exists_but_identity_is_suspicious",
        ))
    if ref.size != part.size:
        issues.append(Issue(
            code="part_size_mismatch",
            severity="error",
            message=f"part {part.idx}: size mismatch (db {part.size} != tg {ref.size})",
            part_idx=part.idx,
            channel_id=part.channel_id,
            message_id=part.message_id,
            doc_id_db=part.doc_id,
            doc_id_tg=ref.doc_id,
            size_db=part.size,
            size_tg=ref.size,
            original_filename_db=part.original_filename,
            caption_expected=_expected_caption(part.original_filename),
            caption_actual=getattr(ref, "caption", None),
            verified_by=verified_by,
            clients_tried=clients_tried,
            recoverability="message_exists_but_size_is_suspicious",
        ))
    if deep:
        expected = _expected_caption(part.original_filename)
        actual = getattr(ref, "caption", None)
        actual_stripped = actual.strip() if isinstance(actual, str) else ""
        actual_filename = _caption_filename(actual)
        accepted_filenames = _caption_filename_candidates(part, logical_filename)
        if expected is None:
            issues.append(Issue(
                code="caption_original_filename_missing",
                severity="warning",
                message=f"part {part.idx}: original filename missing in db; caption cannot be verified",
                part_idx=part.idx,
                channel_id=part.channel_id,
                message_id=part.message_id,
                doc_id_db=part.doc_id,
                doc_id_tg=ref.doc_id,
                size_db=part.size,
                size_tg=ref.size,
                caption_actual=actual,
                verified_by=verified_by,
                clients_tried=clients_tried,
                recoverability="cannot_verify_caption_without_db_original_filename",
            ))
        elif not actual_stripped:
            issues.append(Issue(
                code="caption_missing",
                severity="error",
                message=f"part {part.idx}: caption missing",
                part_idx=part.idx,
                channel_id=part.channel_id,
                message_id=part.message_id,
                doc_id_db=part.doc_id,
                doc_id_tg=ref.doc_id,
                size_db=part.size,
                size_tg=ref.size,
                original_filename_db=part.original_filename,
                caption_expected=expected,
                caption_actual=actual,
                verified_by=verified_by,
                clients_tried=clients_tried,
                recoverability="message_exists_but_metadata_is_incomplete",
            ))
        elif not _caption_filename_matches(actual_filename, accepted_filenames):
            issues.append(Issue(
                code="caption_mismatch",
                severity="error",
                message=f"part {part.idx}: caption mismatch",
                part_idx=part.idx,
                channel_id=part.channel_id,
                message_id=part.message_id,
                doc_id_db=part.doc_id,
                doc_id_tg=ref.doc_id,
                size_db=part.size,
                size_tg=ref.size,
                original_filename_db=part.original_filename,
                caption_expected=expected,
                caption_actual=actual,
                verified_by=verified_by,
                clients_tried=clients_tried,
                recoverability="message_exists_but_metadata_is_suspicious",
                details={"accepted_filenames": accepted_filenames},
            ))
    return issues


async def verify_telegram_parts(
    probes: list[TelegramProbe], parts, *, deep: bool, concurrent: int,
    logical_filename: str | None = None,
) -> list[Issue]:
    """Verify all parts with a global concurrency cap for this file."""
    sem = asyncio.Semaphore(max(1, concurrent))

    async def run_one(job_idx: int, part) -> list[Issue]:
        async with sem:
            return await verify_telegram_part(
                probes, part, deep=deep, offset=job_idx,
                logical_filename=logical_filename,
            )

    batches = await asyncio.gather(*(run_one(i, part) for i, part in enumerate(parts)))
    return [issue for batch in batches for issue in batch]


async def _inspect_db_file(repo, node) -> tuple[Verdict, bool, Any]:
    content = await repo.content_of(node.id)
    parts = await repo.parts_of(node.id)
    path = await repo.path_of(node.id) or node.name
    effective = len(content) if content is not None else sum(p.size for p in parts)
    issues: list[Issue] = []
    if node.size != effective:
        issues.append(Issue(
            code="file_size_mismatch",
            severity="error",
            message=f"file size mismatch: expected {node.size}, effective {effective}",
            size_db=node.size,
            size_tg=effective,
            recoverability="database_parts_do_not_match_node_size",
        ))
    return (
        Verdict(node.id, node.name, path, node.size, effective, len(parts), issues),
        content is None,
        parts,
    )


async def check_file(repo, node, *, probes: list[TelegramProbe] | None = None,
                     deep_telegram: bool = False, concurrent: int = 1) -> Verdict:
    verdict, is_telegram_backed, parts = await _inspect_db_file(repo, node)
    # Telegram-level only for on-Telegram files (inline has no parts)
    if probes and is_telegram_backed:
        verdict.issues.extend(
            await verify_telegram_parts(
                probes, parts, deep=deep_telegram, concurrent=concurrent,
                logical_filename=node.name,
            )
        )
    return verdict


async def walk_files(repo, start_node, depth: int):
    """Yield the file nodes under start_node. A file start yields itself. For a
    folder, descend `depth` folder levels (the start folder is level 1, so
    depth=1 = its direct files only); depth=0 = the whole subtree."""
    if not start_node.is_folder:
        yield start_node
        return
    queue = [(start_node, 1)]
    while queue:
        folder, level = queue.pop(0)
        for child in await repo.children(folder.id):
            if child.is_folder:
                if depth == 0 or level < depth:
                    queue.append((child, level + 1))
            else:
                yield child


async def _log_channel_folder(repo, folder) -> None:
    channel_id = getattr(folder, "channel_id", None)
    if channel_id is None:
        return
    try:
        path = await repo.path_of(folder.id)
    except Exception as exc:  # noqa: BLE001 - progress logging must not abort the check
        log.exception("[check] cannot resolve path for channel folder '%s': %s", folder.name, exc)
        path = folder.name
    log.info("[check] processing channel folder: %s (channel_id=%s)", path or folder.name, channel_id)


async def _channel_folders_under(repo, start_node, depth: int) -> list[Any]:
    """Return channel-root folders to process as sequential work units.

    Once a folder has its own channel_id, its whole subtree belongs to that
    work unit for check progress purposes. This keeps logs aligned with the
    operational folders users monitor.
    """
    if not start_node.is_folder:
        return []
    folders: list[Any] = []
    queue = [(start_node, 1)]
    while queue:
        folder, level = queue.pop(0)
        if getattr(folder, "channel_id", None) is not None:
            folders.append(folder)
            continue
        for child in await repo.children(folder.id):
            if child.is_folder and (depth == 0 or level < depth):
                queue.append((child, level + 1))
    return folders


async def _check_scope(repo, start_node, *, depth: int, probes=None,
                       deep_telegram: bool, concurrent: int,
                       progress_every: int) -> list[Verdict]:
    """Check one sequential work unit and finish its Telegram jobs before returning."""
    verdicts: list[Verdict] = []
    telegram_jobs: list[tuple[Verdict, Any]] = []
    log.info("[check] walking files under '%s' depth=%s", start_node.name, depth)
    async for node in walk_files(repo, start_node, depth):
        try:
            v, is_telegram_backed, parts = await _inspect_db_file(repo, node)
            if probes and is_telegram_backed:
                telegram_jobs.extend((v, part) for part in parts)
        except Exception as exc:  # noqa: BLE001 - keep going; record and report
            path = await repo.path_of(node.id) or node.name
            v = Verdict(
                node.id,
                node.name,
                path,
                node.size,
                -1,
                0,
                [Issue(code="check_error", severity="error", message=f"error: {exc}")],
            )
            log.exception("[check] %s '%s' raised", node.id, node.name)
        verdicts.append(v)
        if len(verdicts) % progress_every == 0:
            log.info(
                "[check] db scan progress: %s file(s), %s telegram part(s)",
                len(verdicts),
                len(telegram_jobs),
            )

    log.info(
        "[check] db scan complete: %s file(s), %s telegram part(s)",
        len(verdicts),
        len(telegram_jobs),
    )

    if probes and telegram_jobs:
        sem = asyncio.Semaphore(max(1, concurrent))
        total_parts = len(telegram_jobs)
        completed_parts = 0
        log.info(
            "[check] telegram verification started: %s part(s), concurrency=%s, probes=%s",
            total_parts,
            max(1, concurrent),
            len(probes),
        )

        async def run_part(job_idx: int, verdict: Verdict, part) -> tuple[Verdict, list[Issue]]:
            nonlocal completed_parts
            async with sem:
                issues = await verify_telegram_part(
                    probes, part, deep=deep_telegram, offset=job_idx,
                    logical_filename=verdict.name,
                )
                for issue in issues:
                    logger = log.error if issue.severity == "error" else log.warning
                    logger(
                        "[check] telegram issue: path=%s part=%s channel_id=%s message_id=%s code=%s message=%s",
                        verdict.path,
                        issue.part_idx,
                        issue.channel_id,
                        issue.message_id,
                        issue.code,
                        issue.message,
                    )
                completed_parts += 1
                if completed_parts % progress_every == 0 or completed_parts == total_parts:
                    log.info(
                        "[check] telegram progress: %s/%s part(s)",
                        completed_parts,
                        total_parts,
                    )
                return verdict, issues

        results = await asyncio.gather(
            *(run_part(i, verdict, part) for i, (verdict, part) in enumerate(telegram_jobs))
        )
        for verdict, issues in results:
            verdict.issues.extend(issues)
    return verdicts


async def check(repo, start_node, *, depth: int = 0, probes=None,
                deep_telegram: bool = False, concurrent: int = 1,
                progress_every: int = 100) -> tuple[list[Verdict], Report]:
    """Walk every file under start_node and check it. NEVER stops at the first
    failure: an exception on one file becomes an error verdict and the walk goes
    on, so the final report covers the whole (sub)tree."""
    progress_every = max(1, progress_every)
    channel_folders = await _channel_folders_under(repo, start_node, depth)
    verdicts: list[Verdict] = []

    if channel_folders:
        for folder in channel_folders:
            await _log_channel_folder(repo, folder)
            verdicts.extend(await _check_scope(
                repo,
                folder,
                depth=0,
                probes=probes,
                deep_telegram=deep_telegram,
                concurrent=concurrent,
                progress_every=progress_every,
            ))
    else:
        verdicts.extend(await _check_scope(
            repo,
            start_node,
            depth=depth,
            probes=probes,
            deep_telegram=deep_telegram,
            concurrent=concurrent,
            progress_every=progress_every,
        ))

    for v in verdicts:
        if not v.ok:
            log.warning(
                "[check] %s '%s': %s",
                v.node_id,
                v.name,
                "; ".join(issue.message for issue in v.issues if issue.severity == "error"),
            )
    report = Report(checked=len(verdicts), bad=sum(1 for v in verdicts if not v.ok))
    log.info("[check] complete: %s", report)
    return verdicts, report


# -- CLI --------------------------------------------------------------------


async def run(db_dsn: str, path: str, *, depth: int, verify_telegram: bool,
              deep_telegram: bool, include_bots: bool, concurrent: int, config_path: str,
              runtime_config=None, progress_every: int = 100) -> tuple[list[Verdict], Report]:
    from tgshelf.db.engine import create_engine, create_session_factory
    from tgshelf.db.repo import NodeRepo

    probes = []
    clients = []
    if verify_telegram:
        probes, clients = await _build_gateways(
            config_path, include_bots=include_bots, runtime_config=runtime_config
        )

    engine = create_engine(db_dsn)
    try:
        async with create_session_factory(engine)() as session:
            start = await NodeRepo(session).resolve(path)
            if start is None:
                raise SystemExit(f"path not found: {path}")
            verdicts, report = await check(
                NodeRepo(session), start, depth=depth, probes=probes,
                deep_telegram=deep_telegram, concurrent=concurrent,
                progress_every=progress_every,
            )
    finally:
        await engine.dispose()
        for client in clients:
            disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
            if disconnect is not None:
                await disconnect()
    return verdicts, report


async def _build_gateways(config_path: str, *, include_bots: bool, runtime_config=None):
    """Connect configured accounts and return (probes, clients).

    By default only USER accounts are used. Bots participate only when explicitly
    requested because missing bot membership should be reported as unreachable,
    not confused with a missing Telegram message.
    """
    from tgshelf.config import load_config
    from tgshelf.http.serve import make_rate_limiter, start_clients

    config = runtime_config or load_config(config_path)
    rl = make_rate_limiter(config.telegram.rate_limit)
    pairs = await start_clients(config, rl)
    clients = [client for _account, client in pairs]
    selected = (
        pairs
        if include_bots
        else [(account, client) for account, client in pairs if not account.is_bot]
    )
    if not selected:
        raise SystemExit("no usable user account; run `tgshelf accounts login <name>`")
    log.info(
        "[check] telegram probes ready: %s selected, %s connected",
        len(selected),
        len(pairs),
    )
    return [TelegramProbe(account.name, client) for account, client in selected], clients


def _print_report(verdicts: list[Verdict], report: Report) -> None:
    print(f"\nintegrity report: {report}")
    for v in verdicts:
        if v.issues:
            print(f"\n{v.path}")
            print(f"  node_id: {v.node_id}")
            print(f"  db_size: {v.expected_size}")
            print(f"  effective_parts_size: {v.effective_size}")
            print(f"  parts: {v.parts_count}")
            for issue in v.issues:
                print(f"\n  [{issue.code}]")
                for key, value in _issue_print_fields(issue):
                    print(f"    {key}: {value}")


def _issue_print_fields(issue: Issue):
    fields = [
        ("severity", issue.severity),
        ("message", issue.message),
        ("part", issue.part_idx),
        ("channel_id", issue.channel_id),
        ("message_id", issue.message_id),
        ("doc_id_db", issue.doc_id_db),
        ("doc_id_tg", issue.doc_id_tg),
        ("size_db", issue.size_db),
        ("size_tg", issue.size_tg),
        ("original_filename_db", issue.original_filename_db),
        ("caption_expected", issue.caption_expected),
        ("caption_actual", issue.caption_actual),
        ("verified_by", issue.verified_by),
        ("clients_tried", ", ".join(issue.clients_tried) if issue.clients_tried else None),
        ("recoverability", issue.recoverability),
    ]
    return [(key, value) for key, value in fields if value not in (None, "", [])]


def _issue_json_record(verdict: Verdict, issue: Issue) -> dict[str, Any]:
    record = asdict(issue)
    record["issue"] = record.pop("code")
    record.update({
        "path": verdict.path,
        "node_id": verdict.node_id,
        "name": verdict.name,
        "expected_size": verdict.expected_size,
        "effective_size": verdict.effective_size,
        "parts_count": verdict.parts_count,
    })
    return record


def _write_jsonl_report(verdicts: list[Verdict], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for verdict in verdicts:
            for issue in verdict.issues:
                fh.write(json.dumps(_issue_json_record(verdict, issue), ensure_ascii=False) + "\n")


def _load_runtime_config(config_path: str):
    from tgshelf.config import load_config

    return load_config(config_path)


def main(argv: list[str] | None = None) -> int:
    from tgshelf.commands.common import resolve_concurrent

    parser = argparse.ArgumentParser(description="Check tgshelf file integrity (DB + optional Telegram).")
    parser.add_argument("path", nargs="?", default="/",
                        help="path of a folder or file to check (default: / = whole tree)")
    parser.add_argument("--db", default=os.environ.get("DB"),
                        help="Postgres DSN override (default: env DB, then config db)")
    parser.add_argument("--depth", type=int, default=0,
                        help="folder navigation depth from the start folder; 0 = infinite (whole subtree)")
    parser.add_argument("--verify-telegram", action="store_true",
                        help="also verify each part still exists on Telegram (doc_id/size)")
    parser.add_argument("--deep-telegram", action="store_true",
                        help="also verify Telegram captions against db original filenames")
    parser.add_argument("--include-bots", action="store_true",
                        help="include configured bot accounts in Telegram verification")
    parser.add_argument("--report-jsonl", help="write a machine-readable issue report as JSONL")
    parser.add_argument("--concurrent", type=int,
                        help="parallel Telegram checks (default: env CONCURRENCY, then config operations.concurrent)")
    parser.add_argument("--progress-every", type=int, default=100,
                        help="log progress every N files/Telegram parts (default: 100)")
    parser.add_argument("--allow-unlimited-telegram", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--config", default="./config.yaml",
                        help="runtime config for db fallback and --verify-telegram")
    parser.add_argument("--log", default="info", help="log level")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    runtime_config = None
    if args.deep_telegram:
        args.verify_telegram = True
    if args.include_bots and not args.verify_telegram:
        parser.error("--include-bots requires --verify-telegram or --deep-telegram")
    if not args.db or args.verify_telegram:
        runtime_config = _load_runtime_config(args.config)
    if runtime_config is None:
        runtime_config = _load_runtime_config(args.config)
    db_dsn = args.db or runtime_config.db
    if not db_dsn:
        parser.error("--db (or env DB, or config db) is required")
    if args.depth < 0:
        parser.error("--depth must be >= 0 (0 = infinite)")
    if args.progress_every < 1:
        parser.error("--progress-every must be >= 1")
    try:
        concurrent = resolve_concurrent(runtime_config, cli_value=getattr(args, "concurrent", None))
    except ValueError as exc:
        parser.error(str(exc))
    log.info(
        "[check] starting path=%s verify_telegram=%s deep_telegram=%s include_bots=%s concurrency=%s",
        args.path,
        args.verify_telegram,
        args.deep_telegram,
        args.include_bots,
        concurrent,
    )

    verdicts, report = asyncio.run(run(
        db_dsn, args.path, depth=args.depth,
        verify_telegram=args.verify_telegram,
        deep_telegram=args.deep_telegram,
        include_bots=args.include_bots,
        concurrent=concurrent,
        config_path=args.config,
        runtime_config=runtime_config,
        progress_every=args.progress_every,
    ))
    _print_report(verdicts, report)
    if args.report_jsonl:
        _write_jsonl_report(verdicts, args.report_jsonl)
    return 1 if report.bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
