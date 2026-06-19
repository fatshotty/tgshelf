"""`tgshelf ls | cp | mv | rm | purge` — thin management wrappers over FileSystem.

Path-addressed (like the rest of the UX). ls/rm touch only Postgres; cp/mv/purge
also need Telegram (forward parts / delete messages), so they connect the user
accounts. The command logic lives in `_do_*` (takes an fs) so it is testable
against a fake-backed fs; `run()` only wires config → fs.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from tgshelf.config import Config
from tgshelf.core.fs import FileSystem
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.db.repo import NodeRepo


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _fmt(node) -> str:
    kind = "d" if node.is_folder else "-"
    size = "" if node.is_folder else str(node.size)
    return f"{kind} {node.id}  {size:>12}  {node.name}"


def _human(size: int) -> str:
    """Render a byte count as a short human-readable string (1024-based)."""
    value = float(size)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if value < 1024 or unit == "P":
            return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"  # unreachable, keeps type checker happy


async def _resolve_any(fs: FileSystem, path: str):
    """Resolve a path, trying ACTIVE first then the trash (DELETED).

    `resolve` matches a single state across the *whole* path, so it handles a
    fully-ACTIVE path and a fully-DELETED one (e.g. an rm'd folder). The common
    case it misses is a single file rm'd inside a folder that stays ACTIVE: the
    path is mixed-state (ACTIVE parent + DELETED leaf). Resolve the parent as
    ACTIVE and pick the trashed child by name so `purge <path>` can reach it."""
    node = await fs.resolve(path)
    if node is not None:
        return node
    node = await fs.repo.resolve(path, state="DELETED")
    if node is not None:
        return node
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    parent = await fs.resolve("/" + "/".join(segments[:-1]))
    if parent is None:
        return None
    return await fs.repo.get_child_by_name(parent.id, segments[-1], state="DELETED")


# -- command logic (testable: operate on a given fs) ------------------------


async def _do_ls(fs: FileSystem, path: str) -> int:
    node = await fs.resolve(path)
    if node is None:
        return _err(f"path not found: {path}")
    if not node.is_folder:
        print(_fmt(node))
        return 0
    for child in await fs.list_children(node.id):
        print(_fmt(child))
    return 0


async def _do_du(fs: FileSystem, path: str) -> int:
    node = await fs.resolve(path)
    if node is None:
        return _err(f"path not found: {path}")
    total = await fs.total_size(node.id)
    print(f"{total}\t{_human(total)}\t{path}")
    return 0


async def _do_mkdir(fs: FileSystem, path: str) -> int:
    # mkdirs semantics (like `mkdir -p`): create missing parents, idempotent if it
    # already exists. DB-only — a folder has no Telegram footprint until files land.
    node = await fs.mkdirs(path)
    print(f"created {path} ({node.id})")
    return 0


async def _do_rm(fs: FileSystem, path: str) -> int:
    node = await fs.resolve(path)
    if node is None:
        return _err(f"path not found: {path}")
    await fs.delete(node.id, purge=False)
    print(f"deleted (soft) {path}")
    return 0


async def _do_purge(fs: FileSystem, path: str) -> int:
    node = await _resolve_any(fs, path)
    if node is None:
        return _err(f"path not found: {path}")
    await fs.delete(node.id, purge=True)
    print(f"purged {path}")
    return 0


async def _do_mv(fs: FileSystem, src: str, dst: str) -> int:
    return await _move_or_copy(fs, src, dst, copy=False)


async def _do_cp(fs: FileSystem, src: str, dst: str) -> int:
    return await _move_or_copy(fs, src, dst, copy=True)


async def _move_or_copy(fs: FileSystem, src: str, dst: str, *, copy: bool) -> int:
    source = await fs.resolve(src)
    if source is None:
        return _err(f"source not found: {src}")
    dest = await fs.resolve(dst)
    if dest is None:
        return _err(f"destination not found: {dst}")
    if not dest.is_folder:
        return _err(f"destination is not a folder: {dst}")
    if copy:
        await fs.copy(source.id, dest.id)
        print(f"copied {src} -> {dst}")
    else:
        await fs.move(source.id, dest.id)
        print(f"moved {src} -> {dst}")
    return 0


# -- config → fs wiring -----------------------------------------------------


@asynccontextmanager
async def _db_fs(config: Config):
    """fs with Postgres only (ls / rm)."""
    engine = create_engine(config.db)
    try:
        async with create_session_factory(engine)() as session:
            yield FileSystem(
                NodeRepo(session),
                master_channel=config.telegram.upload.channel,
                min_size=config.telegram.upload.min_size,
            )
    finally:
        await engine.dispose()


@asynccontextmanager
async def _telegram_fs(config: Config):
    """fs with connected accounts (cp / mv / purge: forward parts / delete msgs)."""
    from tgshelf.http.serve import build_runtime, make_rate_limiter, start_clients

    rate_limiter = make_rate_limiter(config.telegram.rate_limit)
    pairs = await start_clients(config, rate_limiter)
    gateway = next((client for account, client in pairs if not account.is_bot), None)

    engine = create_engine(config.db)
    try:
        session_factory = create_session_factory(engine)
        runtime = build_runtime(config, session_factory, pairs)
        async with session_factory() as session:
            yield FileSystem(
                NodeRepo(session),
                master_channel=config.telegram.upload.channel,
                min_size=config.telegram.upload.min_size,
                uploader=runtime["uploader"],
                streamer=runtime["streamer"],
                executor=runtime["executor"],
                gateway=gateway,
            )
    finally:
        await engine.dispose()
        for _account, client in pairs:
            disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
            if disconnect is not None:
                await disconnect()


async def run(config: Config, args) -> int:
    cmd = args.command
    if cmd == "ls":
        async with _db_fs(config) as fs:
            return await _do_ls(fs, args.path)
    if cmd == "du":
        async with _db_fs(config) as fs:
            return await _do_du(fs, args.path, human=args.human)
    if cmd == "mkdir":
        async with _db_fs(config) as fs:
            return await _do_mkdir(fs, args.path)
    if cmd == "rm":
        async with _db_fs(config) as fs:
            return await _do_rm(fs, args.path)
    if cmd == "purge":
        async with _telegram_fs(config) as fs:
            return await _do_purge(fs, args.path)
    if cmd == "mv":
        async with _telegram_fs(config) as fs:
            return await _do_mv(fs, args.src, args.dst)
    if cmd == "cp":
        async with _telegram_fs(config) as fs:
            return await _do_cp(fs, args.src, args.dst)
    return _err(f"unknown command {cmd}")
