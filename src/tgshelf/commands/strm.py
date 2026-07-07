"""`tgshelf strm` — generate .strm files from the virtual filesystem.

Mirrors the drive tree (from `strm.source`) under `strm.destination`. For each
on-Telegram file it writes `<stem>.strm` containing the configured template with
every placeholder resolved from the node; for inline files (content in the DB) it
writes the raw bytes under the original name (subtitles, .nfo, ...). DB-only -
no Telegram. Placeholders (closed set, validated at config-load):

  {file_id}=node.id  {filename}=node.name  {channel_id}=node.channel_id
  {parts}=part message_ids by idx, comma-joined  {parts_dash}=same, dash-joined
  (URL-safe - no comma, which ffmpeg/Emby reject)  {size}=node.size  {mime}=node.mime
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from tgshelf.config import Config
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.db.repo import NodeRepo

log = logging.getLogger("tgshelf.strm")

PROGRESS_EVERY_FILES = 100


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


@dataclass(frozen=True)
class _ActiveOutput:
    path: Path
    data: bytes
    inline: bool


@dataclass(frozen=True)
class _DeletedOutput:
    path: Path
    data: bytes


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
            # path segment of only [0-9-] - no comma (which ffmpeg/Emby choke on).
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

    Folder -> destination / <node path relative to source> (generate writes its
    contents under it). File -> destination / <parent path relative to source>
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
        if path.is_dir():
            log.error("[strm] cannot write %s: path is a directory", path)
            raise IsADirectoryError(path)
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
    """The output filename + expected bytes for a file node (inline -> raw under
    its name; on-Telegram -> <stem>.strm with the resolved template)."""
    content = await repo.content_of(node.id)
    if content is not None and len(content) > 0:
        return node.name, content
    parts = await repo.parts_of(node.id)
    text = resolve_template(template, node, [p.message_id for p in parts])
    return strm_name(node), text.encode("utf-8")


async def _targets(repo: NodeRepo, nodes, template: str) -> dict[str, tuple[str, bytes]]:
    contents_of = getattr(repo, "contents_of", None)
    parts_by_file = getattr(repo, "parts_by_file", None)
    if not callable(contents_of) or not callable(parts_by_file):
        return {node.id: await _target(repo, node, template) for node in nodes}

    ids = [node.id for node in nodes]
    contents = await contents_of(ids)
    remote_ids = []
    for node in nodes:
        content = contents.get(node.id)
        if content is None or len(content) == 0:
            remote_ids.append(node.id)
    parts = await parts_by_file(remote_ids)
    targets: dict[str, tuple[str, bytes]] = {}
    for node in nodes:
        content = contents.get(node.id)
        if content is not None and len(content) > 0:
            targets[node.id] = (node.name, content)
            continue
        text = resolve_template(
            template,
            node,
            [part.message_id for part in parts.get(node.id, [])],
        )
        targets[node.id] = (strm_name(node), text.encode("utf-8"))
    return targets


async def _release_read_transaction(repo: NodeRepo) -> None:
    session = getattr(repo, "session", None)
    rollback = getattr(session, "rollback", None)
    if rollback is not None:
        await rollback()


def _merge_stats(target: Stats, delta: Stats) -> None:
    target.created += delta.created
    target.updated += delta.updated
    target.skipped += delta.skipped
    target.removed += delta.removed
    target.inline += delta.inline


def _write_active(output: _ActiveOutput) -> Stats:
    stats = Stats()
    _write(output.path, output.data, stats)
    if output.inline:
        stats.inline += 1
    return stats


def _prune_deleted(output: _DeletedOutput) -> Stats:
    stats = Stats()
    path = output.path
    if not path.exists():
        log.debug("[strm] obsolete output already absent %s", path)
    elif path.is_dir():
        log.warning("[strm] keeping obsolete candidate %s: path is a directory", path)
    elif path.read_bytes() == output.data:
        path.unlink()
        stats.removed += 1
        log.debug("[strm] removed obsolete %s", path)
    else:
        log.warning("[strm] keeping obsolete candidate %s: content changed", path)
    return stats


async def _run_local_jobs(
    outputs,
    *,
    concurrent: int,
    worker,
    stats: Stats,
    progress_label: str,
    total: int,
) -> None:
    sem = asyncio.Semaphore(max(1, concurrent))

    async def run_one(output):
        async with sem:
            return await asyncio.to_thread(worker, output)

    tasks = [asyncio.create_task(run_one(output)) for output in outputs]
    completed = 0
    try:
        for task in asyncio.as_completed(tasks):
            _merge_stats(stats, await task)
            completed += 1
            if completed % PROGRESS_EVERY_FILES == 0:
                log.info(
                    "[strm] progress %s=%d/%d (%s)",
                    progress_label,
                    completed,
                    total,
                    stats,
                )
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


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
    active_files = [n for n in files if n.state == "ACTIVE"]
    deleted_files = [n for n in files if n.state == "DELETED"]
    log.info(
        "[strm] discovered %d files (%d active, %d deleted)",
        len(files), len(active_files), len(deleted_files),
    )

    target_by_id = await _targets(repo, files, template)

    active_outputs: list[_ActiveOutput] = []
    for node in active_files:
        name, data = target_by_id[node.id]
        active_outputs.append(
            _ActiveOutput(
                path=destination / rel_dir(node) / name,
                data=data,
                inline=name == node.name,
            )
        )

    deleted_outputs: list[_DeletedOutput] = []
    for node in deleted_files:
        name, data = target_by_id[node.id]
        deleted_outputs.append(_DeletedOutput(destination / rel_dir(node) / name, data))

    await _release_read_transaction(repo)

    # write ACTIVE first, then prune DELETED (so a name reused by an ACTIVE node
    # is already present and the content guard keeps it)
    await _run_local_jobs(
        active_outputs,
        concurrent=concurrent,
        worker=_write_active,
        stats=stats,
        progress_label="active",
        total=len(active_files),
    )

    if deleted_files:
        log.info("[strm] pruning %d deleted outputs", len(deleted_files))
    await _run_local_jobs(
        deleted_outputs,
        concurrent=concurrent,
        worker=_prune_deleted,
        stats=stats,
        progress_label="deleted",
        total=len(deleted_files),
    )

    log.info("[strm] done %s", stats)
    return stats


async def delete_outputs(
    repo: NodeRepo,
    source,
    destination,
    template: str,
    *,
    concurrent: int = 1,
) -> Stats:
    """Remove the local outputs currently generated by `source`.

    Deletion is guarded by expected content, exactly like obsolete-output pruning
    in `generate`: if a file has been edited on disk, it is kept.
    """
    destination = Path(destination)
    log.info("[strm] deleting outputs source=%s destination=%s", source.id, destination)
    if source.is_folder:
        nodes = await repo.subtree(source.id, state=None)
    else:
        nodes = [source]
    by_id = {source.id: source, **{n.id: n for n in nodes}}

    def rel_dir(node) -> Path:
        parts: list[str] = []
        cur = by_id.get(node.parent_id)
        while cur is not None and cur.id != source.id:
            parts.append(cur.name)
            cur = by_id.get(cur.parent_id)
        return Path(*reversed(parts)) if parts else Path()

    files = [n for n in nodes if not n.is_folder]
    target_by_id = await _targets(repo, files, template)
    outputs = [
        _DeletedOutput(destination / rel_dir(node) / target_by_id[node.id][0], target_by_id[node.id][1])
        for node in files
    ]
    await _release_read_transaction(repo)

    stats = Stats()
    await _run_local_jobs(
        outputs,
        concurrent=concurrent,
        worker=_prune_deleted,
        stats=stats,
        progress_label="deleted",
        total=len(outputs),
    )
    log.info("[strm] deleted outputs %s", stats)
    return stats


async def run(config: Config, args) -> int:
    from tgshelf.commands.common import resolve_concurrent

    source_path = getattr(args, "source", None) or config.strm.source
    destination = getattr(args, "destination", None) or config.strm.destination
    clear = bool(getattr(args, "clear", False) or config.strm.clear_folder)
    try:
        concurrent = resolve_concurrent(config, cli_value=getattr(args, "concurrent", None))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    engine = create_engine(config.db)
    try:
        async with create_session_factory(engine)() as session:
            repo = NodeRepo(session)
            source = await repo.resolve(source_path)
            if source is None:
                log.error("[strm] source not found: %s", source_path)
                print(f"error: source not found: {source_path}", file=sys.stderr)
                return 1
            try:
                stats = await generate(
                    repo,
                    source,
                    destination,
                    config.strm.template,
                    clear=clear,
                    concurrent=concurrent,
                )
            except Exception:
                log.exception("[strm] failed source=%s destination=%s", source_path, destination)
                print(f"error: strm generation failed for {source_path} -> {destination}", file=sys.stderr)
                return 1
    finally:
        await engine.dispose()

    print(f"strm: {stats} -> {destination}")
    return 0
