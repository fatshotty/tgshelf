"""Standalone integrity check for tgshelf (NOT a `tgshelf` subcommand).

Two complementary levels (see docs/PLAN.md "Integrità della size"):

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
  python scripts/check_integrity.py [--db <DSN>] [--root <id>]
  python scripts/check_integrity.py --verify-telegram --config ./config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

log = logging.getLogger("tgshelf.check")


@dataclass
class Verdict:
    node_id: str
    name: str
    expected_size: int
    effective_size: int
    issues: list[str] = field(default_factory=list)  # empty = OK

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass
class Report:
    checked: int = 0
    bad: int = 0

    def __str__(self) -> str:
        return f"{self.checked} file(s) checked, {self.bad} with issues"


async def verify_telegram_parts(gateway, parts) -> list[str]:
    """Per-part Telegram verification: message exists + doc_id/size match."""
    issues: list[str] = []
    for p in parts:
        ref = await gateway.get_document(p.channel_id, p.message_id)
        if ref is None:
            issues.append(f"part {p.idx}: message {p.channel_id}/{p.message_id} missing")
            continue
        if p.doc_id is not None and ref.doc_id is not None and ref.doc_id != p.doc_id:
            issues.append(f"part {p.idx}: doc_id mismatch (db {p.doc_id} != tg {ref.doc_id})")
        if ref.size != p.size:
            issues.append(f"part {p.idx}: size mismatch (db {p.size} != tg {ref.size})")
    return issues


async def check_file(repo, node, *, gateway=None) -> Verdict:
    content = await repo.content_of(node.id)
    parts = await repo.parts_of(node.id)
    effective = len(content) if content is not None else sum(p.size for p in parts)
    issues: list[str] = []
    if node.size != effective:
        issues.append(f"size mismatch: expected {node.size}, effective {effective}")
    # Telegram-level only for on-Telegram files (inline has no parts)
    if gateway is not None and content is None:
        issues.extend(await verify_telegram_parts(gateway, parts))
    return Verdict(node.id, node.name, node.size, effective, issues)


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


async def check(repo, start_node, *, depth: int = 0, gateway=None) -> tuple[list[Verdict], Report]:
    """Walk every file under start_node and check it. NEVER stops at the first
    failure: an exception on one file becomes an error verdict and the walk goes
    on, so the final report covers the whole (sub)tree."""
    verdicts: list[Verdict] = []
    async for node in walk_files(repo, start_node, depth):
        try:
            v = await check_file(repo, node, gateway=gateway)
        except Exception as exc:  # noqa: BLE001 - keep going; record and report
            v = Verdict(node.id, node.name, node.size, -1, [f"error: {exc}"])
            log.exception("[check] %s '%s' raised", node.id, node.name)
        if not v.ok:
            log.warning("[check] %s '%s': %s", v.node_id, v.name, "; ".join(v.issues))
        verdicts.append(v)
    report = Report(checked=len(verdicts), bad=sum(1 for v in verdicts if not v.ok))
    return verdicts, report


# -- CLI --------------------------------------------------------------------


async def run(db_dsn: str, path: str, *, depth: int, verify_telegram: bool,
              config_path: str) -> tuple[list[Verdict], Report]:
    from tgshelf.db.engine import create_engine, create_session_factory
    from tgshelf.db.repo import NodeRepo

    gateway = None
    clients = []
    if verify_telegram:
        gateway, clients = await _build_gateway(config_path)

    engine = create_engine(db_dsn)
    try:
        async with create_session_factory(engine)() as session:
            start = await NodeRepo(session).resolve(path)
            if start is None:
                raise SystemExit(f"path not found: {path}")
            verdicts, report = await check(
                NodeRepo(session), start, depth=depth, gateway=gateway
            )
    finally:
        await engine.dispose()
        for client in clients:
            disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
            if disconnect is not None:
                await disconnect()
    return verdicts, report


async def _build_gateway(config_path: str):
    """Connect the configured accounts and return (gateway, clients). The gateway
    is one connected USER client (it must be a member of the channels to read)."""
    from tgshelf.config import load_config
    from tgshelf.http.serve import make_rate_limiter, start_clients

    config = load_config(config_path)
    rl = make_rate_limiter(config.telegram.rate_limit)
    pairs = await start_clients(config, rl)
    clients = [client for _account, client in pairs]
    users = [client for account, client in pairs if not account.is_bot]
    if not clients:
        raise SystemExit("no usable account; run `tgshelf accounts login <name>`")
    return (users or clients)[0], clients


def _print_report(verdicts: list[Verdict], report: Report) -> None:
    print(f"\nintegrity report: {report}")
    for v in verdicts:
        if not v.ok:
            print(f"  ✗ {v.node_id} '{v.name}'")
            for issue in v.issues:
                print(f"      - {issue}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check tgshelf file integrity (DB + optional Telegram).")
    parser.add_argument("path", nargs="?", default="/",
                        help="path of a folder or file to check (default: / = whole tree)")
    parser.add_argument("--db", default=os.environ.get("DB"), help="Postgres DSN (or env DB)")
    parser.add_argument("--depth", type=int, default=0,
                        help="folder navigation depth from the start folder; 0 = infinite (whole subtree)")
    parser.add_argument("--verify-telegram", action="store_true",
                        help="also verify each part still exists on Telegram (doc_id/size)")
    parser.add_argument("--config", default="./config.yaml", help="config for --verify-telegram")
    parser.add_argument("--log", default="info", help="log level")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not args.db:
        parser.error("--db (or env DB) is required")
    if args.depth < 0:
        parser.error("--depth must be >= 0 (0 = infinite)")

    verdicts, report = asyncio.run(run(
        args.db, args.path, depth=args.depth,
        verify_telegram=args.verify_telegram, config_path=args.config,
    ))
    _print_report(verdicts, report)
    return 1 if report.bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
