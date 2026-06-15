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
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"{self.uploaded} uploaded, {self.skipped} skipped, "
            f"{self.mismatched} size-mismatch (skipped), {self.failed} failed"
        )


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


def _fs(session, *, master_channel, min_size, uploader, streamer) -> FileSystem:
    return FileSystem(
        NodeRepo(session), master_channel=master_channel,
        uploader=uploader, streamer=streamer, min_size=min_size,
    )


async def sync(session_factory, uploader, *, master_channel: int, min_size: int,
               local_dir, dest: str = "/", concurrent: int = 1, streamer=None) -> Stats:
    files = scan_local(local_dir)
    stats = Stats()

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

    async def process(lf: LocalFile) -> None:
        async with sem:
            try:
                parent_id = await ensure_folder(lf.rel_dir)
                async with session_factory() as session:
                    fs = make_fs(session)
                    existing = await fs.repo.get_child_by_name(parent_id, lf.name, state="ACTIVE")
                    if existing is not None:
                        if existing.size == lf.size:
                            stats.skipped += 1
                        else:
                            log.warning(
                                "[sync] size mismatch for %s/%s (drive %d != local %d); skipping",
                                _drive_path(dest, lf.rel_dir), lf.name, existing.size, lf.size,
                            )
                            stats.mismatched += 1
                        return
                    await fs.write(parent_id, lf.name, file_source(lf.path))
                stats.uploaded += 1
                log.info("[sync] uploaded %s", lf.name)
            except Exception:  # noqa: BLE001 - one bad file never aborts the run
                stats.failed += 1
                log.exception("[sync] FAILED %s", lf.name)

    await asyncio.gather(*(process(lf) for lf in files))
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
    try:
        session_factory = create_session_factory(engine)
        runtime = build_runtime(config, session_factory, pairs)
        stats = await sync(
            session_factory, runtime["uploader"],
            master_channel=config.telegram.upload.channel,
            min_size=config.telegram.upload.min_size,
            streamer=runtime["streamer"],
            local_dir=local_dir,
            dest=getattr(args, "dest", None) or "/",
            concurrent=getattr(args, "concurrent", None) or config.operations.concurrent,
        )
    finally:
        await engine.dispose()
        for _account, client in pairs:
            disconnect = getattr(getattr(client, "_client", None), "disconnect", None)
            if disconnect is not None:
                await disconnect()

    print(f"sync: {stats}")
    return 1 if stats.failed else 0
