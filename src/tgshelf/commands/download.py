"""`tgshelf download` — download a file or a folder to local disk.

Inverse of `sync`: enumerate the drive subtree, then download files concurrently
(operations.concurrent) through fs.open_read (same multi-bot path as the HTTP
stream). Live per-file progress, resume, an errors log, and a final recap.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncContextManager, Callable

from tgshelf.core.fs import FileSystem
from tgshelf.progress import ProgressState

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
