"""`tgshelf strm` — generate .strm files from the virtual filesystem.

Mirrors the drive tree (from `strm.source`) under `strm.destination`. For each
on-Telegram file it writes `<stem>.strm` containing the configured template with
every placeholder resolved from the node; for inline files (content in the DB) it
writes the raw bytes under the original name (subtitles, .nfo, …). DB-only — no
Telegram. Placeholders (closed set, validated at config-load):

  {file_id}=node.id  {filename}=node.name  {channel_id}=node.channel_id
  {parts}=part message_ids by idx, comma-joined  {parts_dash}=same, dash-joined
  (URL-safe — no comma, which ffmpeg/Emby reject)  {size}=node.size  {mime}=node.mime
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tgshelf.config import Config
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.db.repo import NodeRepo

log = logging.getLogger("tgshelf.strm")


@dataclass
class Stats:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    inline: int = 0

    def __str__(self) -> str:
        return (
            f"{self.created} created, {self.updated} updated, {self.skipped} "
            f"unchanged, {self.removed} removed (obsolete), {self.inline} inline"
        )


def _blank(value) -> str:
    return "" if value is None else str(value)


def resolve_template(template: str, node, part_message_ids) -> str:
    part_ids = [str(m) for m in part_message_ids]
    return template.format_map(
        {
            "file_id": node.id,
            "filename": node.name,
            "channel_id": _blank(node.channel_id),
            "parts": ",".join(part_ids),
            # URL-safe variant: message_ids are integers, so a dash join yields a
            # path segment of only [0-9-] — no comma (which ffmpeg/Emby choke on).
            "parts_dash": "-".join(part_ids),
            "size": node.size,
            "mime": _blank(node.mime),
        }
    )


def strm_name(node) -> str:
    return f"{Path(node.name).stem}.strm"


def strm_base(destination, source_path: str, node_path: str, is_folder: bool) -> Path | None:
    """Where a partial regen of `node_path` writes inside the global destination
    tree, so the output matches a full regen but only that subtree is touched.

    Folder → destination / <node path relative to source> (generate writes its
    contents under it). File → destination / <parent path relative to source>
    (generate writes the single file under it). Returns None if the node is not
    under `source_path`.
    """
    src = [s for s in source_path.split("/") if s]
    segs = [s for s in node_path.split("/") if s]
    if segs[: len(src)] != src:
        return None
    rel = segs[len(src):]
    if not is_folder and rel:
        rel = rel[:-1]  # a file's outputs live in its parent directory
    return Path(destination, *rel)


def _write(path: Path, data: bytes, stats: Stats) -> None:
    """Create the file, or MODIFY it in place when its content changed (never
    delete+recreate); skip when identical."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == data:
            stats.skipped += 1
            log.debug("[strm] unchanged %s", path)
            return
        path.write_bytes(data)  # overwrite in place
        stats.updated += 1
        log.debug("[strm] updated %s", path)
    else:
        path.write_bytes(data)
        stats.created += 1
        log.debug("[strm] created %s", path)


async def _target(repo: NodeRepo, node, template: str) -> tuple[str, bytes]:
    """The output filename + expected bytes for a file node (inline → raw under
    its name; on-Telegram → <stem>.strm with the resolved template)."""
    content = await repo.content_of(node.id)
    if content is not None and len(content) > 0:
        return node.name, content
    parts = await repo.parts_of(node.id)
    text = resolve_template(template, node, [p.message_id for p in parts])
    return strm_name(node), text.encode("utf-8")


def _target_from_maps(
    node,
    template: str,
    contents: dict[str, bytes | None],
    parts_by_file: dict[str, list[Any]],
) -> tuple[str, bytes]:
    content = contents.get(node.id)
    if content is not None and len(content) > 0:
        return node.name, content
    parts = parts_by_file.get(node.id, [])
    text = resolve_template(template, node, [p.message_id for p in parts])
    return strm_name(node), text.encode("utf-8")


async def _contents_by_file(repo: NodeRepo, node_ids: list[str]) -> dict[str, bytes | None]:
    bulk = getattr(repo, "contents_of", None)
    if bulk is not None:
        return await bulk(node_ids)
    return {node_id: await repo.content_of(node_id) for node_id in node_ids}


async def _parts_by_file(repo: NodeRepo, node_ids: list[str]) -> dict[str, list[Any]]:
    bulk = getattr(repo, "parts_by_file", None)
    if bulk is not None:
        return await bulk(node_ids)
    return {node_id: await repo.parts_of(node_id) for node_id in node_ids}


async def _release_read_transaction(repo: NodeRepo) -> None:
    session = getattr(repo, "session", None)
    rollback = getattr(session, "rollback", None)
    if rollback is not None:
        await rollback()


async def _write_outputs(
    jobs: list[tuple[Path, bytes, bool]],
    stats: Stats,
    *,
    concurrent: int,
) -> None:
    async def write_one(path: Path, data: bytes, inline: bool) -> None:
        try:
            await asyncio.to_thread(_write, path, data, stats)
        except OSError:
            log.exception("[strm] cannot write %s", path)
            raise
        if inline:
            stats.inline += 1

    if concurrent <= 1:
        for path, data, inline in jobs:
            await write_one(path, data, inline)
        return

    semaphore = asyncio.Semaphore(concurrent)

    async def guarded(path: Path, data: bytes, inline: bool) -> None:
        async with semaphore:
            await write_one(path, data, inline)

    await asyncio.gather(*(guarded(path, data, inline) for path, data, inline in jobs))


async def generate(
    repo: NodeRepo,
    source,
    destination,
    template: str,
    *,
    clear: bool = False,
    concurrent: int = 1,
) -> Stats:
    """Walk source's subtree and (re)generate outputs under destination.

    ACTIVE files are created/updated in place; DELETED files have their output
    removed (with a content guard, so a name now owned by another node is kept).
    `clear` wipes destination first for a clean regen.
    """
    destination = Path(destination)
    log.info("[strm] starting source=%s destination=%s clear=%s", source.id, destination, clear)
    if clear and destination.exists():
        log.info("[strm] clearing %s", destination)
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    # a folder regenerates its whole subtree; a single file regenerates itself
    # (subtree() returns only descendants, so a file would yield nothing)
    if source.is_folder:
        nodes = await repo.subtree(source.id, state=None)  # all states (paths + cleanup)
    else:
        nodes = [source]
    by_id = {n.id: n for n in nodes}
    stats = Stats()

    def rel_dir(node) -> Path:
        parts: list[str] = []
        cur = by_id.get(node.parent_id)
        while cur is not None and cur.id != source.id:
            parts.append(cur.name)
            cur = by_id.get(cur.parent_id)
        return Path(*reversed(parts)) if parts else Path()

    files = [n for n in nodes if not n.is_folder]
    log.info("[strm] discovered %d files", len(files))
    file_ids = [node.id for node in files]
    contents = await _contents_by_file(repo, file_ids)
    parts_by_file = await _parts_by_file(repo, file_ids)

    active_jobs: list[tuple[Path, bytes, bool]] = []
    deleted_jobs: list[tuple[Path, bytes]] = []
    active_total = sum(1 for node in files if node.state == "ACTIVE")
    active_seen = 0
    for node in files:
        name, data = _target_from_maps(node, template, contents, parts_by_file)
        path = destination / rel_dir(node) / name
        if node.state == "ACTIVE":
            active_jobs.append((path, data, name == node.name))
            active_seen += 1
            if active_seen % 100 == 0:
                log.info("[strm] progress active=%d/%d", active_seen, active_total)
        elif node.state == "DELETED":
            deleted_jobs.append((path, data))

    await _release_read_transaction(repo)

    # write ACTIVE first, then prune DELETED (so a name reused by an ACTIVE node
    # is already present and the content guard keeps it)
    await _write_outputs(active_jobs, stats, concurrent=max(1, int(concurrent)))

    for path, data in deleted_jobs:
        if path.exists() and path.read_bytes() == data:  # guard: still ours
            path.unlink()
            stats.removed += 1
            log.debug("[strm] removed obsolete %s", path)
        elif path.exists():
            log.warning("[strm] keeping obsolete candidate %s (content differs)", path)

    log.info("[strm] done %s", stats)
    return stats


async def delete_outputs(repo: NodeRepo, source, destination, template: str) -> Stats:
    """Delete the generated output files for one source file or folder.

    The content guard mirrors obsolete pruning in `generate`: a file is removed
    only when the bytes on disk still match the output this node would generate.
    Manual edits or files now owned by something else are left untouched.
    """
    destination = Path(destination)
    if source.is_folder:
        nodes = await repo.subtree(source.id, state=None)
    else:
        nodes = [source]
    by_id = {source.id: source, **{n.id: n for n in nodes}}
    stats = Stats()

    def rel_dir(node) -> Path:
        parts: list[str] = []
        cur = by_id.get(node.parent_id)
        while cur is not None and cur.id != source.id:
            parts.append(cur.name)
            cur = by_id.get(cur.parent_id)
        return Path(*reversed(parts)) if parts else Path()

    for node in [n for n in nodes if not n.is_folder]:
        name, data = await _target(repo, node, template)
        path = destination / rel_dir(node) / name
        if not path.exists():
            stats.skipped += 1
            continue
        if path.is_file() and path.read_bytes() == data:
            path.unlink()
            stats.removed += 1
            log.debug("[strm] removed output %s", path)
        else:
            stats.skipped += 1
            log.warning("[strm] keeping obsolete candidate %s (content differs)", path)

    log.info("[strm] deleted %s", stats)
    return stats


async def run(config: Config, args) -> int:
    source_path = getattr(args, "source", None) or config.strm.source
    destination = getattr(args, "destination", None) or config.strm.destination
    clear = bool(getattr(args, "clear", False) or config.strm.clear_folder)
    concurrent = int(getattr(args, "concurrent", 1) or 1)

    engine = create_engine(config.db)
    try:
        async with create_session_factory(engine)() as session:
            repo = NodeRepo(session)
            source = await repo.resolve(source_path)
            if source is None:
                print(f"error: source not found: {source_path}", file=sys.stderr)
                return 1
            stats = await generate(
                repo,
                source,
                destination,
                config.strm.template,
                clear=clear,
                concurrent=concurrent,
            )
    finally:
        await engine.dispose()

    print(f"strm: {stats} → {destination}")
    return 0
