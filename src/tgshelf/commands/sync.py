"""`tgshelf sync` — upload a local directory tree into the drive.

Mirrors a local folder under a drive folder, creating the tree and uploading the
files concurrently (up to `operations.concurrent`). A file already present with
the **same size** is skipped (so a re-run resumes naturally); a size **mismatch**
is logged and skipped — sync never overwrites an existing file (decisione utente,
legacy-knowledge §11).

Uploads do NOT go through the FsExecutor: the Uploader already leases an account
per file, so the executor would double-lease. Concurrency here is a plain
semaphore; each upload gets its own session + FileSystem (sessions are not
concurrent-safe).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

from tgshelf.config import Config
from tgshelf.constants import CHUNK_SIZE
from tgshelf.core.fs import FileSystem
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.db.repo import NodeRepo
from tgshelf.progress import ProgressState, format_recap
from tgshelf.render import run_render_loop

log = logging.getLogger("tgshelf.sync")


@dataclass
class LocalFile:
    rel_dir: tuple[str, ...]  # directories from the scan root to the file
    name: str
    path: Path
    size: int


@dataclass
class Stats:
    uploaded: int = 0
    skipped: int = 0
    mismatched: int = 0
    overwritten: int = 0
    deleted: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"{self.uploaded} uploaded, {self.skipped} skipped, "
            f"{self.mismatched} size-mismatch (skipped), "
            f"{self.overwritten} overwritten, {self.deleted} deleted, {self.failed} failed"
        )


def prune_empty_dirs(start_dir: Path, stop_root: Path) -> int:
    """Remove empty directories from `start_dir` upward, stopping BEFORE
    `stop_root` (the scan root is never removed). Best-effort: a non-empty dir
    (OSError) or one already removed by another worker (FileNotFoundError) ends
    the walk. Returns how many dirs were removed."""
    removed = 0
    current = start_dir
    while current != stop_root and stop_root in current.parents:
        try:
            current.rmdir()  # raises OSError if not empty
        except (FileNotFoundError, OSError):
            break
        removed += 1
        current = current.parent
    return removed


def scan_local(local_dir) -> list[LocalFile]:
    root = Path(local_dir)
    files: list[LocalFile] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root)
        files.append(LocalFile(rel.parent.parts, path.name, path, path.stat().st_size))
    return files


def _drive_path(dest: str, rel_dir: tuple[str, ...]) -> str:
    segments = [s for s in dest.split("/") if s] + list(rel_dir)
    return "/" + "/".join(segments)


def file_source(path: Path, *, chunk: int = CHUNK_SIZE) -> Callable[[], AsyncIterator[bytes]]:
    def factory() -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            with open(path, "rb") as f:
                while True:
                    block = f.read(chunk)
                    if not block:
                        break
                    yield block

        return gen()

    return factory


def counting_source(path: Path, key: str, state: ProgressState, *, chunk: int = CHUNK_SIZE):
    """Like file_source, but reports progress: each chunk read advances `state`
    for `key`. Counts bytes read from disk (≈ bytes pushed into the upload engine)."""
    def factory() -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            with open(path, "rb") as f:
                while True:
                    block = f.read(chunk)
                    if not block:
                        break
                    state.advance(key, len(block))
                    yield block

        return gen()

    return factory


def recap_extra(stats: "Stats") -> str:
    """Sync-only recap line for counters the shared format_recap doesn't carry."""
    return f"+ {stats.overwritten} overwritten, {stats.deleted} deleted"


def _fs(session, *, master_channel, min_size, uploader, streamer) -> FileSystem:
    return FileSystem(
        NodeRepo(session), master_channel=master_channel,
        uploader=uploader, streamer=streamer, min_size=min_size,
    )


async def sync(session_factory, uploader, *, master_channel: int, min_size: int,
               local_dir, dest: str = "/", concurrent: int = 1, streamer=None,
               delete_source: bool = False, overwrite: bool = False,
               state: ProgressState | None = None) -> Stats:
    root_dir = Path(local_dir)
    files = scan_local(local_dir)
    stats = Stats()
    st = state or ProgressState(len(files), sum(f.size for f in files))

    def make_fs(session) -> FileSystem:
        return _fs(session, master_channel=master_channel, min_size=min_size,
                   uploader=uploader, streamer=streamer)

    # The reference folder is created WHEN a file is processed, not up front. To
    # stay race-safe when concurrent workers need the same (or a prefix-sharing)
    # folder, folder creation is serialised + memoised by a shared lock+cache —
    # it's DB-only and fast, so uploads stay parallel.
    folder_lock = asyncio.Lock()
    folder_cache: dict[tuple[str, ...], str] = {}

    async def ensure_folder(rel_dir: tuple[str, ...]) -> str:
        async with folder_lock:
            if rel_dir not in folder_cache:
                async with session_factory() as session:
                    folder = await make_fs(session).mkdirs(_drive_path(dest, rel_dir))
                    folder_cache[rel_dir] = folder.id
            return folder_cache[rel_dir]

    sem = asyncio.Semaphore(max(1, concurrent))

    async def process(idx: int, lf: LocalFile) -> None:
        key = str(idx)
        async with sem:
            st.start_file(key, lf.name, lf.size)
            try:
                parent_id = await ensure_folder(lf.rel_dir)
                async with session_factory() as session:
                    fs = make_fs(session)
                    existing = await fs.repo.get_child_by_name(parent_id, lf.name, state="ACTIVE")
                    if existing is not None and not overwrite:
                        if existing.size == lf.size:
                            stats.skipped += 1
                        else:
                            log.warning(
                                "[sync] size mismatch for %s/%s (drive %d != local %d); skipping",
                                _drive_path(dest, lf.rel_dir), lf.name, existing.size, lf.size,
                            )
                            stats.mismatched += 1
                        st.finish(key, "skipped")
                        return
                    replaced = existing is not None and overwrite
                    node = await fs.write(parent_id, lf.name,
                                          counting_source(lf.path, key, st), overwrite=overwrite)
                if replaced:
                    stats.overwritten += 1
                    log.info("[sync] overwritten %s", lf.name)
                else:
                    stats.uploaded += 1
                    log.info("[sync] uploaded %s", lf.name)
                st.finish(key, "ok")
                if delete_source:
                    if node.size != lf.size:
                        log.warning(
                            "[sync] not deleting source %s: size %d != node %d",
                            lf.path, lf.size, node.size,
                        )
                    else:
                        try:
                            lf.path.unlink()
                            stats.deleted += 1
                            prune_empty_dirs(lf.path.parent, root_dir)
                            log.info("[sync] deleted source %s", lf.path)
                        except OSError:
                            log.warning("[sync] could not delete source %s", lf.path)
            except Exception:  # noqa: BLE001 - one bad file never aborts the run
                stats.failed += 1
                st.finish(key, "failed")
                log.exception("[sync] FAILED %s", lf.name)

    await asyncio.gather(*(process(i, lf) for i, lf in enumerate(files)))
    log.info("[sync] %s", stats)
    return stats


async def run(config: Config, args) -> int:
    local_dir = Path(args.local_dir)
    if not local_dir.is_dir():
        print(f"error: not a directory: {local_dir}", file=sys.stderr)
        return 1

    from tgshelf.http.serve import build_runtime, make_rate_limiter, start_clients

    rate_limiter = make_rate_limiter(config.telegram.rate_limit)
    pairs = await start_clients(config, rate_limiter)
    engine = create_engine(config.db)

    dest = getattr(args, "dest", None) or "/"
    log_path = getattr(args, "log_file", None)
    header = f"sync {local_dir} -> {dest}"

    files = scan_local(local_dir)
    state = ProgressState(len(files), sum(f.size for f in files))

    file_handler = None
    propagated = log.propagate
    debug = log.isEnabledFor(logging.DEBUG)
    block = sys.stdout.isatty() and not debug  # live block owns stdout on a TTY

    if log_path:
        file_handler = logging.FileHandler(log_path)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(file_handler)
        if log.level == logging.NOTSET or log.level > logging.INFO:
            log.setLevel(logging.INFO)
        log.info("[sync] === run %s -> %s (%d files) ===", local_dir, dest, len(files))

    # On a TTY the live block owns stdout; keep the per-file [sync] lines off screen
    # (they go to the file handler if any) by not propagating to the root logger.
    if block:
        log.propagate = False
        print(header)

    stop = asyncio.Event()
    renderer = (asyncio.create_task(
        run_render_loop(state, header=header, stop=stop, log=log, marker="sync"))
        if block else None)

    stats = Stats()
    try:
        session_factory = create_session_factory(engine)
        runtime = build_runtime(config, session_factory, pairs)
        stats = await sync(
            session_factory, runtime["uploader"],
            master_channel=config.telegram.upload.channel,
            min_size=config.telegram.upload.min_size,
            streamer=runtime["streamer"],
            local_dir=local_dir,
            dest=dest,
            concurrent=getattr(args, "concurrent", None) or config.operations.concurrent,
            delete_source=getattr(args, "delete_source", False),
            overwrite=getattr(args, "overwrite", False),
            state=state,
        )
    finally:
        stop.set()
        if renderer is not None:
            await renderer
        log.propagate = propagated
        await engine.dispose()
        for _account, client in pairs:
            disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
            if disconnect is not None:
                await disconnect()
        if file_handler is not None:
            log.info("[sync] === done: %s ===", stats)
            log.removeHandler(file_handler)
            file_handler.close()

    print(format_recap(state.snapshot()))
    print(recap_extra(stats))
    if log_path:
        print(f"log written to {log_path}")
    return 1 if stats.failed else 0
