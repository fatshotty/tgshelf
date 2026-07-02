"""`tgshelf download` — download a file or a folder to local disk.

Inverse of `sync`: enumerate the drive subtree, then download files concurrently
(operations.concurrent) through fs.open_read (same multi-bot path as the HTTP
stream). Live per-file progress, resume, an errors log, and a final recap.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncContextManager, Callable

from tgshelf.config import Config
from tgshelf.core.fs import FileSystem
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.db.repo import NodeRepo
from tgshelf.log import new_request_id
from tgshelf.looplag import start_loop_lag_monitor, stop_loop_lag_monitor
from tgshelf.progress import (
    ProgressState, format_recap, error_header, error_line, error_footer,
)
from tgshelf.render import run_render_loop

log = logging.getLogger("tgshelf.download")


@dataclass(frozen=True)
class PlannedFile:
    id: str
    rel_path: str  # path relative to the requested root (POSIX-style, "/"-joined)
    size: int


async def plan_download(fs: FileSystem, path: str) -> list[PlannedFile]:
    """Enumerate the ACTIVE files to download. A single file -> one entry with its
    bare name; a folder -> every ACTIVE file in the subtree with its path relative
    to that folder. No skip decision here. Raises FileNotFoundError if missing."""
    root = await fs.resolve(path)
    if root is None:
        raise FileNotFoundError(path)
    if not root.is_folder:
        return [PlannedFile(id=root.id, rel_path=root.name, size=root.size)]

    base = await fs.path_of(root.id)  # e.g. "/movies"
    base_prefix = base if base.endswith("/") else base + "/"
    out: list[PlannedFile] = []
    for node in await fs.walk(root.id, state="ACTIVE"):
        if node.is_folder:
            continue
        full = await fs.path_of(node.id)         # "/movies/2024/deep.bin"
        rel = full[len(base_prefix):] if full.startswith(base_prefix) else node.name
        out.append(PlannedFile(id=node.id, rel_path=rel, size=node.size))
    return out


async def download_file(
    fs: FileSystem, node_id: str, local_path: Path, state: ProgressState,
    *, key: str, overwrite: bool,
) -> str:
    """Download one file to `local_path`, updating `state`. Returns the status:
    "ok" | "skipped". Creates the parent dir on-demand. Without overwrite: skip if
    the local size already matches, resume if it is smaller. Raises ValueError on a
    size-mismatch sanity failure (the caller turns that into a "failed")."""
    new_request_id()  # tag this file's fetches (+ streamer workers) in the log
    node = await fs.get(node_id)
    size = node.size
    state.start_file(key, node.name, size)

    await asyncio.to_thread(local_path.parent.mkdir, parents=True, exist_ok=True)

    start = 0
    mode = "wb"
    if not overwrite and local_path.exists():
        have = local_path.stat().st_size
        if have == size:
            state.finish(key, "skipped")
            return "skipped"
        if 0 < have < size:
            start, mode = have, "ab"          # resume: append the rest
            state.advance(key, have)          # count already-present bytes

    written = start
    with open(local_path, mode) as fh:
        async for chunk in fs.open_read(node_id, start=start):
            await asyncio.to_thread(fh.write, chunk)
            written += len(chunk)
            state.advance(key, len(chunk))

    if written != size:                       # sanity: must match the expected size
        state.finish(key, "failed")
        raise ValueError(f"size mismatch for {node.name}: wrote {written}, expected {size}")
    state.finish(key, "ok")
    return "ok"


@dataclass
class DownloadResult:
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (rel_path, reason)


async def download_tree(
    fs: FileSystem, path: str, dest, *, concurrent: int, overwrite: bool,
    fs_factory: Callable[[], AsyncContextManager[FileSystem]],
    state: ProgressState | None = None,
) -> DownloadResult:
    """Plan + download every file concurrently. `fs_factory()` is an async context
    manager yielding a fresh fs (its own DB session) for ONE file — sessions are
    not concurrent-safe; the context manager opens/closes it. `state` (optional) is
    updated for the renderer; created internally if omitted. Local layout mirrors
    the drive name: dest/<root-name>/<rel> for a folder, dest/<name> for a single
    file. Never aborts on a per-file failure."""
    dest = Path(dest)
    files = await plan_download(fs, path)
    root = await fs.resolve(path)
    # folder -> nest under dest/<folder-name>; single file -> straight into dest
    base = dest / root.name if root.is_folder else dest

    st = state or ProgressState(len(files), sum(f.size for f in files))
    result = DownloadResult()
    sem = asyncio.Semaphore(max(1, concurrent))

    async def worker(idx: int, pf: PlannedFile) -> None:
        local = base / pf.rel_path
        async with sem:
            try:
                async with fs_factory() as wfs:
                    status = await download_file(
                        wfs, pf.id, local, st, key=str(idx), overwrite=overwrite,
                    )
                if status == "ok":
                    result.ok += 1
                elif status == "skipped":
                    result.skipped += 1
            except Exception as exc:  # noqa: BLE001 - one bad file never aborts
                result.failed += 1
                result.errors.append((pf.rel_path, str(exc)))
                log.error("[download] FAILED %s: %s", pf.rel_path, exc)

    await asyncio.gather(*(worker(i, pf) for i, pf in enumerate(files)))
    return result


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_error_log(path: Path, drive_path: str, total: int,
                     result: DownloadResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(error_header(_ts(), drive_path, total) + "\n")
        for rel, reason in result.errors:
            fh.write(error_line(_ts(), rel, reason) + "\n")
        fh.write(error_footer(result.ok, result.skipped, result.failed) + "\n")


async def run(config: Config, args) -> int:
    from tgshelf.commands.common import resolve_concurrent
    from tgshelf.http.serve import build_runtime, make_rate_limiter, start_clients

    dest = Path(getattr(args, "dest", None) or ".")
    try:
        concurrent = resolve_concurrent(config, cli_value=getattr(args, "concurrent", None))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    overwrite = bool(getattr(args, "overwrite", False))

    rate_limiter = make_rate_limiter(config.telegram.rate_limit)
    pairs = await start_clients(config, rate_limiter)
    engine = create_engine(config.db)
    try:
        session_factory = create_session_factory(engine)
        runtime = build_runtime(config, session_factory, pairs)

        def make_fs(session) -> FileSystem:
            return FileSystem(
                NodeRepo(session), master_channel=config.telegram.upload.channel,
                streamer=runtime["streamer"], min_size=config.telegram.upload.min_size,
            )

        # one fresh DB session per worker (sessions are not concurrent-safe), same
        # pattern as sync.process; the streamer (bot pool) is shared.
        @asynccontextmanager
        async def worker_fs():
            async with session_factory() as session:
                yield make_fs(session)

        # plan with a dedicated read session (to size the progress + header)
        async with session_factory() as session:
            files = await plan_download(make_fs(session), args.path)

        header = (f"download {args.path}  —  boost multi_bot_download="
                  f"{config.download.multi_bot_download}, {concurrent} concurrent")
        # At debug the per-chunk logs ARE the live view; the ANSI block would fight
        # them on the same stream, so suppress it. On a TTY the render loop owns the
        # whole block (header included), so print the header only when there is no
        # block to draw it (non-TTY, or debug).
        debug = log.isEnabledFor(logging.DEBUG)
        if debug or not sys.stdout.isatty():
            print(header)
        state = ProgressState(len(files), sum(f.size for f in files))

        stop = asyncio.Event()
        renderer = None if debug else asyncio.create_task(
            run_render_loop(state, header=header, stop=stop, log=log, marker="download"))
        # at debug, watch for event-loop stalls that would freeze every fetch at once
        lag_task = start_loop_lag_monitor() if debug else None
        try:
            async with session_factory() as read_session:
                result = await download_tree(
                    make_fs(read_session), args.path, dest,
                    concurrent=concurrent, overwrite=overwrite,
                    fs_factory=worker_fs, state=state,
                )
        finally:
            stop.set()
            if renderer is not None:
                await renderer
            await stop_loop_lag_monitor(lag_task)

        print(format_recap(state.snapshot()))
        if result.errors:
            log_path = Path(getattr(args, "log_file", None)
                            or (dest / "tgshelf-download-errors.log"))
            _write_error_log(log_path, args.path, len(files), result)
            print(f"{result.failed} errors logged to {log_path}")
    finally:
        await engine.dispose()
        for _account, client in pairs:
            disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
            if disconnect is not None:
                await disconnect()

    return 1 if result.failed else 0
