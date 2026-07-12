"""FileSystem facade: the single API over the drive.

One surface for HTTP / CLI / bot / future mount, addressable by id or path,
nothing HTTP-shaped. It orchestrates NodeRepo (tree), the pools and the
download/upload/channels engines in transactions. This module is built up across
A8: reads + effective channel first, then write/open_read, tree ops, move/copy,
import/merge.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Iterable, Sequence

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from tgshelf.constants import ROOT_ID
from tgshelf.core import channels
from tgshelf.core.captions import (
    DEFAULT_CAPTION_TEMPLATE,
    CaptionRenderContext,
    caption_template_depends_on,
    render_caption,
)
from tgshelf.core.download import RangeNotSatisfiable, StreamPlan
from tgshelf.core.mirror import MirrorPlan, build_mirror_plan
from tgshelf.core.upload import PartRecord
from tgshelf.db.models import Node
from tgshelf.db.repo import DuplicateNameError, NodeRepo

log = logging.getLogger("tgshelf.fs")

DEFAULT_MIME = "application/octet-stream"
INFO_NOTES_MAX_LENGTH = 200

# structural mime validation: type/subtype as RFC tokens, optional ";params".
# Not a closed registry (new types appear); just rejects garbage (no slash,
# empty side, spaces, multiple slashes).
_MIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_+.-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_+.-]*(\s*;.*)?$")


@dataclass(frozen=True)
class _PurgeFilePlan:
    file_id: str
    name: str
    path: str
    telegram_parts: int
    channels: int
    parts: tuple[Any, ...]


def is_valid_mime(mime: str) -> bool:
    return bool(_MIME_RE.match(mime))


def _info_notes(node: Any) -> str:
    info = getattr(node, "info", None)
    if not isinstance(info, dict):
        return ""
    notes = info.get("notes", "")
    return notes if isinstance(notes, str) else ""


class NotAReadableFile(Exception):
    """The node is missing, a folder, or not ACTIVE."""


class NotAFolder(Exception):
    """The destination of a move/copy exists but is not a folder."""


class InlineTooLarge(Exception):
    """An in-place content edit would push an inline (DB-stored) file past the
    inline threshold (min_size). The caller must opt in (force) to convert it to
    a Telegram-backed file instead — surfaced as 409 by the HTTP layer."""


class IntegrityViolation(Exception):
    """A file's expected size (node.size, frozen when the content is defined)
    disagrees with its effective size (inline = len(content); parts =
    sum(parts.size)). Signals a lost/corrupted part row or a denormalisation
    bug — surfaced by check_size, never raised on the read path."""


class InfoNotesTooLong(ValueError):
    """The free-form notes field exceeds the domain cap."""


class FileSystem:
    def __init__(
        self,
        repo: NodeRepo,
        *,
        master_channel: int,
        uploader: Any = None,
        streamer: Any = None,
        gateway: Any = None,
        min_size: int = 0,
        executor: Any = None,
        notifier: Any = None,
        caption_template: str = DEFAULT_CAPTION_TEMPLATE,
        plugin_manager: Any = None,
    ):
        self.repo = repo
        self._master_channel = master_channel
        self._uploader = uploader
        self._streamer = streamer
        self._gateway = gateway  # user client for management ops (delete/forward)
        self._min_size = min_size
        # optional Notifier: critical move/copy cleanup failures get pushed to the
        # alert channel (see channels.delete_originals); None -> log-only.
        self._notifier = notifier
        self._caption_template = caption_template
        self._plugin_manager = plugin_manager
        # FsExecutor: when set, folder move/copy fan out per-file (each on its own
        # session + leased account). Without it, the fallback runs sequentially on
        # this instance's session/gateway.
        self._executor = executor

    # -- reads / navigation -------------------------------------------------

    async def get(self, node_id: str) -> Node | None:
        return await self.repo.get(node_id)

    async def resolve(self, path: str) -> Node | None:
        return await self.repo.resolve(path)

    async def path_of(self, node_id: str) -> str | None:
        return await self.repo.path_of(node_id)

    async def list_children(
        self,
        node_id: str,
        *,
        state: str | None = "ACTIVE",
        folders_only: bool = False,
        files_only: bool = False,
    ) -> Sequence[Node]:
        return await self.repo.children(
            node_id, state=state, folders_only=folders_only, files_only=files_only
        )

    async def walk(self, node_id: str, *, state: str | None = None) -> Sequence[Node]:
        return await self.repo.subtree(node_id, state=state)

    async def search(self, term: str, *, root_id: str | None = None) -> Sequence[Node]:
        return await self.repo.search(term, root_id=root_id)

    async def total_size(self, node_id: str, *, state: str = "ACTIVE") -> int:
        """Total byte size of a node: a file's own size, or the recursive sum of
        all ACTIVE files in a folder's subtree. Raises if the node is missing."""
        node = await self.repo.get(node_id)
        if node is None:
            raise NotAReadableFile(f"node {node_id} not found")
        if not node.is_folder:
            return node.size
        return await self.repo.subtree_size(node_id, state=state)

    # -- channel resolution -------------------------------------------------

    async def effective_channel(self, node_id: str, *, skip_current: bool = False) -> int:
        node = await self.repo.get(node_id)
        if node is None:
            return self._master_channel
        ancestors = await self.repo.ancestors(node_id)
        return channels.effective_channel(
            node, ancestors, self._master_channel, skip_current=skip_current
        )

    # -- tree ops -----------------------------------------------------------

    async def mkdir(self, parent_id: str, name: str) -> Node:
        node = await self.repo.create(
            name=name, parent_id=parent_id, is_folder=True, state="ACTIVE"
        )
        await self.repo.session.commit()
        return await self.repo.get(node.id)

    async def mkdirs(self, path: str) -> Node:
        """mkdir -p: create missing folders along `path`, reusing existing ones.

        Idempotent under concurrent creation: if another instance/operation
        creates the same segment between our read and our write, the create loses
        with DuplicateNameError — we re-query and reuse the folder it created
        instead of failing the whole `mkdir -p` (the multi-instance model makes
        this race real). A name taken by a *file* still surfaces the error."""
        current = ROOT_ID
        for segment in (s for s in path.split("/") if s):
            current = await self._mkdir_or_reuse(current, segment)
        return await self.repo.get(current)

    async def _mkdir_or_reuse(self, parent_id: str, segment: str) -> str:
        match = await self._find_child_folder(parent_id, segment)
        if match is not None:
            return match.id
        try:
            created = await self.repo.create(
                name=segment, parent_id=parent_id, is_folder=True, state="ACTIVE"
            )
            await self.repo.session.commit()
            return created.id
        except DuplicateNameError:
            # lost the race (or a same-name node appeared): reuse it if it is a
            # folder, otherwise the name is genuinely taken -> re-raise.
            match = await self._find_child_folder(parent_id, segment)
            if match is None:
                raise
            return match.id

    async def _find_child_folder(self, parent_id: str, segment: str) -> Node | None:
        children = await self.repo.children(parent_id, folders_only=True)
        return next((n for n in children if n.name.lower() == segment.lower()), None)

    async def _available_child_name(
        self, parent_id: str, desired: str, *, excluding: set[str] | None = None
    ) -> str:
        excluding = excluding or set()
        siblings = await self.repo.children(parent_id, state="ACTIVE")
        taken = {n.name.lower() for n in siblings if n.id not in excluding}
        if desired.lower() not in taken:
            return desired

        for i in range(1, 10000):
            candidate = f"{desired} ({i})"
            if candidate.lower() not in taken:
                return candidate
        raise DuplicateNameError(f"cannot allocate a unique name for {desired!r}")

    async def rename(self, node_id: str, new_name: str) -> Node:
        node = await self.repo.get(node_id)
        old_mime = getattr(node, "mime", None) if node is not None else None
        hooks_enabled = self._plugins_enabled()
        old_path = new_path = None
        if hooks_enabled and node is not None:
            old_path = await self.path_of(node_id)
            new_path = await self._logical_child_path(node.parent_id, new_name)
        if hooks_enabled and node is not None and not node.is_folder:
            await self._run_before_plugin(
                "before_file_rename",
                operation="file_rename",
                node=node,
                old_parent_id=node.parent_id,
                new_parent_id=node.parent_id,
                old_path=old_path,
                new_path=new_path,
            )
        fields: dict[str, Any] = {"name": new_name}
        if node is not None and not node.is_folder:
            fields["mime"] = mimetypes.guess_type(new_name)[0] or DEFAULT_MIME
        try:
            await self.repo.set_fields(node_id, **fields)
            await self.repo.session.commit()
        except IntegrityError as exc:
            await self.repo.session.rollback()
            raise DuplicateNameError(f"name '{new_name}' already exists") from exc
        changed = {"filename"}
        if fields.get("mime") != old_mime:
            changed.add("mime")
        await self._sync_part_captions(node_id, changed_fields=changed)
        updated = await self.repo.get(node_id)
        if hooks_enabled and node is not None and not node.is_folder:
            await self._run_after_plugin(
                "after_file_rename",
                operation="file_rename",
                node=updated,
                old_parent_id=node.parent_id,
                new_parent_id=node.parent_id,
                old_path=old_path,
                new_path=new_path,
            )
        return updated

    async def set_channel(self, node_id: str, channel_id: int | None) -> Node:
        node = await self.repo.get(node_id)
        if node is None:
            raise NotAReadableFile(f"node {node_id} not found")
        # set_channel applies to FOLDERS only (set an override, or NULL = inherit).
        # A file's channel is where its parts physically live: it can ONLY change
        # via move(), which forwards the parts. Changing it here would desync the
        # node from its messages.
        if not node.is_folder:
            raise ValueError("a file's channel can only be changed by moving it")
        await self.repo.set_fields(node_id, channel_id=channel_id)
        await self.repo.session.commit()
        return await self.repo.get(node_id)

    async def set_mime(self, node_id: str, mime: str | None) -> Node:
        """Set a file's mime; an empty/None mime is deduced from its filename.
        A non-empty user mime must be structurally valid (type/subtype)."""
        node = await self.repo.get(node_id)
        if node is None:
            raise NotAReadableFile(f"node {node_id} not found")
        if mime:
            if not is_valid_mime(mime):
                raise ValueError(f"invalid mime type: {mime!r}")
            resolved = mime
        else:
            resolved = mimetypes.guess_type(node.name)[0] or DEFAULT_MIME
        await self.repo.set_fields(node_id, mime=resolved)
        await self.repo.session.commit()
        await self._sync_part_captions(node_id, changed_fields={"mime"})
        return await self.repo.get(node_id)

    async def set_info_notes(self, node_id: str, notes: str) -> Node:
        """Set nodes.info["notes"] and resync captions that render {info}."""
        node = await self.repo.get(node_id)
        if node is None or node.is_folder or node.state != "ACTIVE":
            raise NotAReadableFile(f"node {node_id} is not a readable file")
        if not isinstance(notes, str):
            raise ValueError("info.notes must be a string")
        if len(notes) > INFO_NOTES_MAX_LENGTH:
            raise InfoNotesTooLong(
                f"info.notes exceeds {INFO_NOTES_MAX_LENGTH} characters"
            )
        info = dict(node.info or {})
        info["notes"] = notes
        await self.repo.set_fields(node_id, info=info, mtime=func.now())
        await self.repo.session.commit()
        await self._sync_part_captions(node_id, changed_fields={"info"})
        return await self.repo.get(node_id)

    async def resync_caption(self, node_id: str) -> None:
        """Force a Telegram caption render for every part of a Telegram-backed file."""
        await self._sync_part_captions(node_id)

    async def delete(
        self, node_id: str, *, purge: bool = False, deleted_only: bool = False
    ) -> None:
        if not purge:
            hooks_enabled = self._plugins_enabled()
            files = await self._file_hook_nodes_for_delete(node_id) if hooks_enabled else []
            hook_paths = (
                {file.id: await self.path_of(file.id) for file in files}
                if hooks_enabled
                else {}
            )
            if hooks_enabled:
                for file in files:
                    await self._run_before_plugin(
                        "before_file_delete",
                        operation="file_delete",
                        node=file,
                        old_parent_id=file.parent_id,
                        old_path=hook_paths[file.id],
                    )
            await self.repo.set_state_subtree(node_id, "DELETED", from_states=("ACTIVE", "TEMP"))
            await self.repo.session.commit()
            if hooks_enabled:
                for file in files:
                    await self._run_after_plugin(
                        "after_file_delete",
                        operation="file_delete",
                        node=await self.repo.get(file.id),
                        old_parent_id=file.parent_id,
                        old_path=hook_paths[file.id],
                    )
            return
        # purge: remove the Telegram messages of every file in the subtree first.
        # deleted_only restricts both the Telegram deletion and the row removal to
        # the subtree's DELETED nodes — the ACTIVE tree (and the root) survive. It
        # is the manual cleanup of a backup's discards after a mirror run.
        state = "DELETED" if deleted_only else None
        node = await self.repo.get(node_id)
        node_path = await self.repo.path_of(node_id)
        parts = await self.repo.parts_in_subtree(node_id, state=state)
        files = await self.repo.purge_file_summaries(node_id, state=state)
        purge_plan = _build_purge_file_plan(files, parts)
        log.info(
            "[purge] starting node=%s path=%s name=%s deleted_only=%s telegram_parts=%d gateway=%s",
            node_id,
            node_path or "?",
            getattr(node, "name", "?"),
            deleted_only,
            len(parts),
            "yes" if self._gateway is not None or self._executor is not None else "no",
        )
        for file_plan in purge_plan:
            log.info(
                "[purge] file node=%s path=%s name=%s telegram_parts=%d channels=%d",
                file_plan.file_id,
                file_plan.path,
                file_plan.name,
                file_plan.telegram_parts,
                file_plan.channels,
            )
        if parts and (self._gateway is not None or self._executor is not None):
            _validate_purge_plan(parts, purge_plan)
        if parts and self._executor is not None:
            channels_count = len({part.channel_id for part in parts})
            log.info(
                "[purge] deleting %d Telegram-backed part messages across %d channels using executor",
                len(parts),
                channels_count,
            )
            _raise_first_error(await self._executor.run(purge_plan, _purge_file_op))
        elif parts and self._gateway is not None:
            channels_count = len({part.channel_id for part in parts})
            log.info(
                "[purge] deleting %d Telegram-backed part messages across %d channels",
                len(parts),
                channels_count,
            )
            await _delete_purge_file_plans(self._gateway, purge_plan)
        elif parts:
            log.warning("[purge] no gateway configured; %d Telegram messages were not deleted", len(parts))
        log.info(
            "[purge] deleting database subtree node=%s state=%s",
            node_id,
            state or "ANY",
        )
        await self.repo.purge_subtree(node_id, state=state)
        await self.repo.session.commit()
        log.info(
            "[purge] done node=%s path=%s name=%s deleted_only=%s telegram_parts=%d",
            node_id,
            node_path or "?",
            getattr(node, "name", "?"),
            deleted_only,
            len(parts),
        )

    async def restore(self, node_id: str) -> None:
        try:
            await self.repo.set_state_subtree(node_id, "ACTIVE", from_states=("DELETED",))
            await self.repo.session.commit()
        except IntegrityError as exc:
            await self.repo.session.rollback()
            raise DuplicateNameError(
                f"cannot restore {node_id}: an active sibling has the same name"
            ) from exc

    async def mirror(self, source_id: str, dest_id: str, *, dry_run: bool = False) -> MirrorPlan:
        source = await self.repo.get(source_id)
        if source is None or not source.is_folder or source.state != "ACTIVE":
            raise NotAReadableFile(f"source folder {source_id} not found")
        dest = await self.repo.get(dest_id)
        if dest is None or not dest.is_folder or dest.state != "ACTIVE":
            raise NotAReadableFile(f"destination folder {dest_id} not found")
        if source.id == dest.id:
            raise ValueError("source and destination cannot be the same folder")

        source_ancestors = await self.repo.ancestors(source.id)
        dest_ancestors = await self.repo.ancestors(dest.id)
        if any(node.id == source.id for node in dest_ancestors) or any(
            node.id == dest.id for node in source_ancestors
        ):
            raise ValueError("source and destination cannot be inside each other")

        plan = await build_mirror_plan(self.repo, source, dest)
        log.info(
            "[mirror] planned source=%s dest=%s actions=%d dry_run=%s",
            source_id, dest_id, len(plan.actions), dry_run,
        )
        if dry_run:
            return plan

        await self._apply_mirror_plan(plan)
        log.info(
            "[mirror] applied source=%s dest=%s actions=%d",
            source_id, dest_id, len(plan.actions),
        )
        return plan

    async def _apply_mirror_plan(self, plan: MirrorPlan) -> None:
        delete_kinds = {
            "delete_extra",
            "replace_file",
            "replace_folder_with_file",
            "replace_file_with_folder",
        }
        for action in sorted(
            [
                action for action in plan.actions
                if action.kind in delete_kinds and action.dest_id
            ],
            key=lambda action: (-_path_depth(action.path), action.path.lower()),
        ):
            await self.delete(action.dest_id, purge=False)

        for action in sorted(
            [
                action for action in plan.actions
                if action.kind in {"create_folder", "replace_file_with_folder"}
            ],
            key=lambda action: (_path_depth(action.path), action.path.lower()),
        ):
            parent_id = await self._mirror_parent_id(plan.dest_id, action.path)
            name = action.path.rsplit("/", 1)[-1]
            await self._ensure_folder(parent_id, name)

        for action in sorted(
            [
                action for action in plan.actions
                if action.kind in {"copy_file", "replace_file", "replace_folder_with_file"}
            ],
            key=lambda action: (_path_depth(action.path), action.path.lower()),
        ):
            if action.source_id is None:
                raise NotAReadableFile(f"mirror source for {action.path} not found")
            source = await self.repo.get(action.source_id)
            if source is None or source.is_folder:
                raise NotAReadableFile(f"source file {action.source_id} not found")
            parent_id = await self._mirror_parent_id(plan.dest_id, action.path)
            await self._copy_file(source, parent_id)

    async def _mirror_parent_id(self, dest_root_id: str, action_path: str) -> str:
        parent_path = action_path.rpartition("/")[0]
        if not parent_path:
            return dest_root_id
        parent_id = dest_root_id
        for segment in parent_path.split("/"):
            node = await self.repo.get_child_by_name(parent_id, segment, state="ACTIVE")
            if node is None or not node.is_folder:
                raise NotAReadableFile(f"mirror destination folder {parent_path} not found")
            parent_id = node.id
        return parent_id

    # -- move ---------------------------------------------------------------

    async def ensure_move_target(self, new_parent_id: str) -> None:
        """Validate a move/copy destination before any DB write: it must exist,
        be ACTIVE and be a folder. Fail-fast with a precise error instead of
        letting a stale/file target surface as a misleading FK/duplicate error."""
        parent = await self.repo.get(new_parent_id)
        if parent is None or parent.state != "ACTIVE":
            raise NotAReadableFile(f"destination folder {new_parent_id} not found")
        if not parent.is_folder:
            raise NotAFolder(f"destination {new_parent_id} is not a folder")

    async def move(self, node_id: str, new_parent_id: str) -> Node:
        """Move a node to a new parent. A file (or a folder's descendant files)
        whose effective channel changes has its parts physically forwarded to
        the new channel; a same-channel move is just a reparent."""
        node = await self.repo.get(node_id)
        if node is None:
            raise NotAReadableFile(f"node {node_id} not found")
        await self.ensure_move_target(new_parent_id)
        name, is_folder = node.name, node.is_folder  # before any rollback expires node
        hooks_enabled = self._plugins_enabled()
        move_hook_contexts: list[tuple[Any, str | None, str | None]] = []
        if hooks_enabled:
            old_path = await self.path_of(node_id)
            new_path = await self._logical_child_path(new_parent_id, node.name)
            move_hook_contexts = (
                await self._folder_move_hook_contexts(node, old_path, new_path)
                if is_folder
                else [(node, old_path, new_path)]
            )
            for file, file_old_path, file_new_path in move_hook_contexts:
                await self._run_before_plugin(
                    "before_file_move",
                    operation="file_move",
                    node=file,
                    old_parent_id=file.parent_id,
                    new_parent_id=file.parent_id if is_folder else new_parent_id,
                    old_path=file_old_path,
                    new_path=file_new_path,
                )

        try:
            await self.repo.set_fields(node_id, parent_id=new_parent_id)
            await self.repo.session.commit()
        except IntegrityError as exc:
            await self.repo.session.rollback()
            raise DuplicateNameError(
                f"a node named '{name}' already exists in the destination"
            ) from exc

        if is_folder:
            items = [n.id for n in await self.repo.subtree(node_id, state="ACTIVE") if not n.is_folder]
        else:
            items = [node_id]
        results = await self._fan_out(items, _reroute_op)
        if is_folder:
            _log_failures(results, "move")
        else:
            _raise_first_error(results)  # single file: surface the error
        updated = await self.repo.get(node_id)
        if hooks_enabled:
            for file, file_old_path, file_new_path in move_hook_contexts:
                await self._run_after_plugin(
                    "after_file_move",
                    operation="file_move",
                    node=await self.repo.get(file.id),
                    old_parent_id=file.parent_id,
                    new_parent_id=file.parent_id if is_folder else new_parent_id,
                    old_path=file_old_path,
                    new_path=file_new_path,
                )
        return updated

    async def _fan_out(self, items, op) -> list:
        """Run a per-file op across `items`: parallel via the executor (each on
        its own session + leased account) when present, else sequentially here.
        Returns one result (or captured exception) per item — even a single file
        runs through the executor when present, so it is always account-leased."""
        if self._executor is not None:
            return await self._executor.run(items, op)
        results: list = []
        for item in items:
            try:
                results.append(await op(self, item))
            except Exception as exc:  # noqa: BLE001 - per-item isolation
                results.append(exc)
        return results

    async def _reroute_file(self, file: Node) -> None:
        """Physically relocate a file's parts if its effective channel changed."""
        source = file.channel_id
        dest = await self.effective_channel(file.id, skip_current=True)
        if dest == source:
            await self._sync_part_captions(file.id, changed_fields={"path"})
            return

        parts = await self.repo.parts_of(file.id)
        if not parts:  # inline file: just move the channel pointer
            await self.repo.set_fields(file.id, channel_id=dest)
            await self.repo.session.commit()
            return

        total = len(parts)
        parent_path = await self._parent_path(file)
        captions = {
            (p.channel_id, p.message_id): self._render_part_caption(
                file, p, i, total, parent_path=parent_path, channel_id=dest
            )
            for i, p in enumerate(parts)
        }
        new_parts = await channels.forward_parts(
            self._gateway,
            parts,
            dest,
            caption_factory=lambda p: captions[(p.channel_id, p.message_id)],
        )
        await self.repo.clear_parts(file.id)
        for np in new_parts:
            await self.repo.add_part(
                file.id, idx=np.idx, channel_id=np.channel_id, message_id=np.message_id,
                doc_id=np.doc_id, size=np.size, original_filename=np.original_filename,
            )
        await self.repo.set_fields(file.id, channel_id=dest)
        await self.repo.session.commit()  # commit BEFORE deleting originals (crash-safe)

        await channels.delete_originals(
            self._gateway, [p for p in parts if p.channel_id != dest],
            notifier=self._notifier,
        )

    # -- copy ---------------------------------------------------------------

    async def copy(
        self,
        node_id: str,
        new_parent_id: str,
        *,
        force_copy: bool = False,
    ) -> Node:
        """Copy a node under `new_parent_id`, leaving the source untouched.
        Telegram messages are duplicated so the copy owns its own messages."""
        node = await self.repo.get(node_id)
        if node is None:
            raise NotAReadableFile(f"node {node_id} not found")
        await self.ensure_move_target(new_parent_id)
        if node.is_folder:
            return await self._copy_folder(node, new_parent_id, force_copy=force_copy)
        existing = await self._existing_file(new_parent_id, node.name)
        log.debug(
            "[copy] requested file '%s' (%s) -> parent %s force_copy=%s",
            node.name, node.id, new_parent_id, force_copy,
        )
        if existing is not None and not force_copy:
            log.debug(
                "[copy] skip existing file '%s' (%s) in parent %s for source %s",
                existing.name, existing.id, new_parent_id, node.id,
            )
            return existing
        # single file: also via the executor (solution B) -> account-leased
        results = await self._fan_out([(node_id, new_parent_id, force_copy)], _copy_op)
        new_id = _first_result(results)
        return await self.repo.get(new_id)

    async def copy_files(
        self,
        node_ids: Iterable[str],
        new_parent_id: str,
        *,
        force_copy: bool = False,
    ) -> list[Node]:
        await self.ensure_move_target(new_parent_id)
        pairs: list[tuple[str, str, bool]] = []
        reused: list[Node] = []
        existing_files = await self._existing_files_by_name(new_parent_id)
        for node_id in node_ids:
            node = await self.repo.get(node_id)
            if node is None or node.is_folder:
                continue
            existing = existing_files.get(node.name.lower())
            log.debug(
                "[copy] requested file '%s' (%s) -> parent %s force_copy=%s",
                node.name, node.id, new_parent_id, force_copy,
            )
            if existing is not None and not force_copy:
                log.debug(
                    "[copy] skip existing file '%s' (%s) in parent %s for source %s",
                    existing.name, existing.id, new_parent_id, node.id,
                )
                reused.append(existing)
                continue
            pairs.append((node.id, new_parent_id, force_copy))
        results = await self._fan_out(pairs, _copy_op) if pairs else []
        _raise_first_error(results)
        created = [await self.repo.get(node_id) for node_id in results]
        return reused + [node for node in created if node is not None]

    async def _dedup_name(self, parent_id: str, name: str, *, is_folder: bool) -> str:
        children = await self.repo.children(parent_id)
        taken = {c.name.lower() for c in children}
        if name.lower() not in taken:
            return name
        if is_folder:
            stem, suffix = name, ""
        else:
            dot = name.rfind(".")
            stem, suffix = (name, "") if dot <= 0 else (name[:dot], name[dot:])
        i = 1
        while f"{stem} - {i}{suffix}".lower() in taken:
            i += 1
        return f"{stem} - {i}{suffix}"

    async def _existing_file(self, parent_id: str, name: str) -> Node | None:
        existing = await self.repo.get_child_by_name(parent_id, name, state="ACTIVE")
        if existing is not None and not existing.is_folder:
            return existing
        return None

    async def _existing_files_by_name(self, parent_id: str) -> dict[str, Node]:
        children = await self.repo.children(parent_id, files_only=True)
        return {child.name.lower(): child for child in children}

    async def _copy_file(self, src: Node, dst_parent_id: str, *, force_copy: bool = False) -> Node:
        dest_channel = await self.effective_channel(dst_parent_id)
        name = await self._dedup_name(dst_parent_id, src.name, is_folder=False)
        hooks_enabled = self._plugins_enabled()
        old_path = new_path = None
        source_plugin_node = None
        if hooks_enabled:
            old_path = await self.path_of(src.id)
            new_path = await self._logical_child_path(dst_parent_id, name)
            source_plugin_node = await self._plugin_node_snapshot(src.id)
            await self._run_before_plugin(
                "before_file_copy",
                operation="file_copy",
                node=src,
                old_parent_id=src.parent_id,
                new_parent_id=dst_parent_id,
                old_path=old_path,
                new_path=new_path,
                source_node=source_plugin_node,
            )

        content = await self.repo.content_of(src.id)
        if content is not None:
            new = await self.repo.create(
                name=name, parent_id=dst_parent_id, is_folder=False, mime=src.mime,
                channel_id=dest_channel, state="ACTIVE", size=len(content), content=content,
                info=dict(getattr(src, "info", None) or {}),
            )
            await self.repo.session.commit()
            log.info(
                "[copy] copied file '%s' (%s) -> parent %s as '%s' (%s) force_copy=%s",
                src.name, src.id, dst_parent_id, new.name, new.id, force_copy,
            )
            copied = await self.repo.get(new.id)
            if hooks_enabled:
                await self._run_after_plugin(
                    "after_file_copy",
                    operation="file_copy",
                    node=copied,
                    old_parent_id=src.parent_id,
                    new_parent_id=dst_parent_id,
                    old_path=old_path,
                    new_path=await self.path_of(new.id),
                    source_node=source_plugin_node,
                    target_node=await self._plugin_node_snapshot(new.id),
                )
            return copied

        src_parts = await self.repo.parts_of(src.id)
        new = await self.repo.create(
            name=name, parent_id=dst_parent_id, is_folder=False, mime=src.mime,
            channel_id=dest_channel, state="TEMP", info=dict(getattr(src, "info", None) or {}),
        )
        await self.repo.session.commit()
        total = len(src_parts)
        parent_path = await self._path_of_folder(dst_parent_id)
        captions = {
            (p.channel_id, p.message_id): self._render_part_caption_values(
                node_id=new.id,
                parent_path=parent_path,
                logical_name=name,
                idx=i,
                total_parts=total,
                part=p,
                mime=src.mime,
                info_notes=_info_notes(src),
                channel_id=dest_channel,
            )
            for i, p in enumerate(src_parts)
        }
        new_parts = await channels.forward_parts(
            self._gateway,
            src_parts,
            dest_channel,
            always_copy=True,
            caption_factory=lambda p: captions[(p.channel_id, p.message_id)],
        )
        for np in new_parts:
            await self.repo.add_part(
                new.id, idx=np.idx, channel_id=np.channel_id, message_id=np.message_id,
                doc_id=np.doc_id, size=np.size, original_filename=np.original_filename,
            )
        await self.repo.set_fields(
            new.id, state="ACTIVE", size=sum(p.size for p in new_parts)
        )
        await self.repo.session.commit()
        log.info(
            "[copy] copied file '%s' (%s) -> parent %s as '%s' (%s) force_copy=%s",
            src.name, src.id, dst_parent_id, name, new.id, force_copy,
        )
        copied = await self.repo.get(new.id)
        if hooks_enabled:
            await self._run_after_plugin(
                "after_file_copy",
                operation="file_copy",
                node=copied,
                old_parent_id=src.parent_id,
                new_parent_id=dst_parent_id,
                old_path=old_path,
                new_path=await self.path_of(new.id),
                source_node=source_plugin_node,
                target_node=await self._plugin_node_snapshot(new.id),
            )
        return copied

    async def _ensure_folder(self, parent_id: str, name: str) -> str:
        """Reuse a same-name folder under parent (merge) or create it; return id."""
        existing = await self.repo.children(parent_id, folders_only=True)
        match = next((c for c in existing if c.name.lower() == name.lower()), None)
        if match is not None:
            return match.id
        created = await self.repo.create(
            name=name, parent_id=parent_id, is_folder=True, state="ACTIVE"
        )
        await self.repo.session.commit()
        return created.id

    async def _copy_folder(
        self,
        src: Node,
        dst_parent_id: str,
        *,
        force_copy: bool = False,
    ) -> Node:
        mapping = {src.id: await self._ensure_folder(dst_parent_id, src.name)}
        descendants = await self.repo.subtree(src.id, state="ACTIVE")  # shallow-first

        # pass 1: recreate the folder structure (parents before children)
        for node in descendants:
            if node.is_folder:
                mapping[node.id] = await self._ensure_folder(mapping[node.parent_id], node.name)

        # pass 2: copy the files into their mapped folders (fanned out)
        existing_by_parent: dict[str, dict[str, Node]] = {}
        pairs: list[tuple[str, str, bool]] = []
        for node in descendants:
            if node.is_folder:
                continue
            parent_id = mapping[node.parent_id]
            if parent_id not in existing_by_parent:
                existing_by_parent[parent_id] = await self._existing_files_by_name(parent_id)
            existing = existing_by_parent[parent_id].get(node.name.lower())
            log.debug(
                "[copy] requested file '%s' (%s) -> parent %s force_copy=%s",
                node.name, node.id, parent_id, force_copy,
            )
            if existing is not None and not force_copy:
                log.debug(
                    "[copy] skip existing file '%s' (%s) in parent %s for source %s",
                    existing.name, existing.id, parent_id, node.id,
                )
                continue
            pairs.append((node.id, parent_id, force_copy))
        _log_failures(await self._fan_out(pairs, _copy_op), "copy")
        return await self.repo.get(mapping[src.id])

    # -- write --------------------------------------------------------------

    async def write(
        self,
        parent_id: str,
        name: str,
        source_factory: Callable[[], AsyncIterator[bytes]],
        *,
        mime: str | None = None,
        overwrite: bool = False,
        on_account: Callable[[str], Any] | None = None,
    ) -> Node:
        """Upload a file under `parent_id`, converging the three upload paths
        (CLI sync / mount PUT / webui) into one place.

        With `overwrite=True`, a colliding ACTIVE sibling is soft-deleted and the
        new node activated in one commit AT FINALIZE (safe swap): a failed upload
        leaves the old file untouched. Default False — other callers are unaffected.

        A TEMP node is created first (invisible to readers); the Uploader streams
        to the parent's effective channel persisting each portion's parts row as
        it finalizes (crash-safe); on success the node flips ACTIVE, on any
        failure the TEMP node is dropped (parts cascade; the Uploader already
        deleted the finalized Telegram messages).
        """
        parent = await self.repo.get(parent_id)
        if parent is None or not parent.is_folder:
            raise NotAReadableFile(f"parent {parent_id} is not a folder")

        channel = await self.effective_channel(parent_id)
        mime = mime or mimetypes.guess_type(name)[0] or DEFAULT_MIME

        node = await self.repo.create(
            name=name, parent_id=parent_id, is_folder=False,
            mime=mime, channel_id=channel, state="TEMP",
        )
        await self.repo.session.commit()
        if self._plugins_enabled():
            try:
                await self._run_before_plugin(
                    "before_file_upload",
                    operation="file_upload",
                    node=node,
                    new_parent_id=parent_id,
                    new_path=await self._logical_child_path(parent_id, name),
                )
            except Exception:
                await self.repo.purge(node.id)
                await self.repo.session.commit()
                raise

        async def persist_part(rec: PartRecord) -> None:
            await self.repo.add_part(
                node.id, idx=rec.idx, channel_id=rec.channel_id,
                message_id=rec.message_id, doc_id=rec.doc_id,
                size=rec.size, original_filename=rec.original_filename,
            )
            await self.repo.session.commit()

        async def reset_parts() -> None:
            await self.repo.clear_parts(node.id)
            await self.repo.session.commit()

        try:
            result = await self._uploader.upload(
                source_factory, filename=name, mime=mime, channel_id=channel,
                min_size=self._min_size, on_part=persist_part, on_reset=reset_parts,
                on_account=on_account, caption_template=self._caption_template,
                node_id=node.id, parent_path=await self._path_of_folder(parent_id),
            )
        except BaseException:
            await self.repo.purge(node.id)  # drop TEMP (parts cascade)
            await self.repo.session.commit()
            raise

        # Safe swap: only now that the upload succeeded do we remove a colliding
        # ACTIVE sibling, so a failed upload (handled above) never loses the old
        # file. Soft-delete + activate happen in one commit; the old node is
        # DELETED before the new becomes ACTIVE, so the partial unique index
        # (uq_nodes_parent_lower_name_active) is always satisfied.
        if overwrite:
            old = await self.repo.get_child_by_name(parent_id, name, state="ACTIVE")
            if old is not None and old.id != node.id:
                await self.repo.set_state_subtree(old.id, "DELETED", from_states=("ACTIVE", "TEMP"))
        if result.inline_content is not None:
            await self.repo.set_fields(
                node.id, content=result.inline_content, size=result.size, state="ACTIVE"
            )
        else:
            await self.repo.set_fields(node.id, size=result.size, state="ACTIVE")
        await self.repo.session.commit()
        # post-write sanity: the persisted parts must sum back to the expected
        # size (catches a part row that failed to persist). Always holds for a
        # correct write; a guard, not a recovery path.
        await self.check_size(node.id)
        if self._plugins_enabled():
            uploaded = await self.repo.get(node.id)
            await self._run_after_plugin(
                "after_file_upload",
                operation="file_upload",
                node=uploaded,
                new_path=await self.path_of(node.id),
            )
        return await self.repo.get(node.id)

    async def replace_content(
        self, node_id: str, data: bytes, *, force: bool = False
    ) -> Node:
        """Overwrite the body of an INLINE (DB-stored) file in place.

        Editing is restricted to inline files (Telegram-backed ones would mean
        re-chunking + re-uploading the whole file → use overwrite upload). The
        new body keeps the file inline while it stays within `min_size`; once it
        grows past the threshold the file must become Telegram-backed, which only
        happens with `force=True` (otherwise InlineTooLarge → 409). `mtime` is
        bumped so the /download ETag and the rclone VFS invalidate.
        """
        node = await self.repo.get(node_id)
        if node is None or node.is_folder or node.state != "ACTIVE":
            raise NotAReadableFile(f"node {node_id} is not a readable file")
        if await self.repo.content_of(node_id) is None:
            raise ValueError(
                "only inline (DB-stored) files can be edited; "
                f"file {node_id} is Telegram-backed"
            )

        if len(data) <= self._min_size:
            await self.repo.set_fields(
                node_id, content=data, size=len(data), mtime=func.now()
            )
            await self.repo.session.commit()
            return await self.repo.get(node_id)

        # The edit overflows the inline threshold: convert to Telegram-backed.
        if not force:
            raise InlineTooLarge(
                f"content ({len(data)} bytes) exceeds the inline limit "
                f"({self._min_size}); pass force to store it on Telegram"
            )
        if self._uploader is None:
            raise ValueError("uploader unavailable: cannot store content on Telegram")

        channel = node.channel_id or await self.effective_channel(node.parent_id)

        async def persist_part(rec: PartRecord) -> None:
            await self.repo.add_part(
                node_id, idx=rec.idx, channel_id=rec.channel_id,
                message_id=rec.message_id, doc_id=rec.doc_id,
                size=rec.size, original_filename=rec.original_filename,
            )
            await self.repo.session.commit()

        async def reset_parts() -> None:
            await self.repo.clear_parts(node_id)
            await self.repo.session.commit()

        def source_factory() -> AsyncIterator[bytes]:
            async def gen() -> AsyncIterator[bytes]:
                yield data

            return gen()

        result = await self._uploader.upload(
            source_factory, filename=node.name, mime=node.mime, channel_id=channel,
            min_size=self._min_size, on_part=persist_part, on_reset=reset_parts,
            caption_template=self._caption_template,
            node_id=node_id,
            parent_path=await self._path_of_folder(node.parent_id),
        )
        # Drop the inline body now that the parts are durable; size follows the
        # parts. (force-path data is always > min_size, so it never stays inline.)
        await self.repo.set_fields(
            node_id, content=None, size=result.size, mtime=func.now()
        )
        await self.repo.session.commit()
        await self._sync_part_captions(node_id)
        await self.check_size(node_id)
        return await self.repo.get(node_id)

    # -- integrity ----------------------------------------------------------

    async def check_size(self, node_id: str) -> bool:
        """Verify a file's expected size equals its effective size.

        expected = node.size (frozen at upload/merge); effective = len(content)
        for inline files, sum(parts.size) for Telegram-backed ones. Folders have
        no size and always pass. Mismatch (or a missing node) ⇒ IntegrityViolation.

        Cheap and DB-only (no Telegram round-trips): meant for an on-demand fsck,
        NOT the read path. It does NOT detect a deleted Telegram message — the
        part row (and its size) survives that — which needs the separate, heavier
        Telegram-level verify.
        """
        node = await self.repo.get(node_id)
        if node is None:
            raise IntegrityViolation(f"node {node_id} not found")
        if node.is_folder:
            return True
        content = await self.repo.content_of(node_id)
        if content is not None:
            effective = len(content)
        else:
            effective = await self.repo.parts_size(node_id)
        if node.size != effective:
            raise IntegrityViolation(
                f"size mismatch for {node_id}: expected {node.size}, effective {effective}"
            )
        return True

    # -- plugin hooks -------------------------------------------------------

    def _plugins_enabled(self) -> bool:
        manager = self._plugin_manager
        return manager is not None and bool(getattr(manager, "enabled", False))

    async def _run_before_plugin(
        self,
        hook_name: str,
        *,
        operation: str,
        node: Node | None,
        **context_fields: Any,
    ) -> None:
        manager = self._plugin_manager
        if node is None or not self._plugins_enabled():
            return
        ctx = await self._plugin_context(
            operation=operation,
            node=node,
            **context_fields,
        )
        if ctx is not None:
            await manager.run_before(hook_name, ctx)

    async def _run_after_plugin(
        self,
        hook_name: str,
        *,
        operation: str,
        node: Node | None,
        **context_fields: Any,
    ) -> None:
        manager = self._plugin_manager
        if node is None or not self._plugins_enabled():
            return
        ctx = await self._plugin_context(
            operation=operation,
            node=node,
            **context_fields,
        )
        if ctx is not None:
            await manager.run_after(hook_name, ctx)

    async def _plugin_context(
        self,
        *,
        operation: str,
        node: Node,
        **context_fields: Any,
    ):
        from tgshelf.plugins import PluginContext

        plugin_node = await self._plugin_node_snapshot(node.id)
        if plugin_node is None:
            return None
        return PluginContext(
            operation=operation,
            host=await self._plugin_host(),
            node=plugin_node,
            **context_fields,
        )

    async def _plugin_host(self):
        from tgshelf.plugins import PluginHost

        return PluginHost(self)

    async def _plugin_node_snapshot(self, node_id: str):
        host = await self._plugin_host()
        return await host.get_node(node_id)

    async def _logical_child_path(self, parent_id: str | None, name: str) -> str:
        parent_path = await self._path_of_folder(parent_id)
        return f"/{name}" if parent_path == "/" else f"{parent_path}/{name}"

    async def _folder_move_hook_contexts(
        self, folder: Node, old_folder_path: str | None, new_folder_path: str | None
    ) -> list[tuple[Node, str | None, str | None]]:
        descendants = await self.repo.subtree(folder.id, state="ACTIVE")
        files = [node for node in descendants if not node.is_folder]
        contexts = []
        for file in files:
            old_file_path = await self.path_of(file.id)
            new_file_path = None
            if old_file_path is not None and old_folder_path and new_folder_path:
                suffix = old_file_path[len(old_folder_path):]
                new_file_path = f"{new_folder_path}{suffix}"
            contexts.append((file, old_file_path, new_file_path))
        return contexts

    async def _file_hook_nodes_for_delete(self, node_id: str) -> list[Node]:
        node = await self.repo.get(node_id)
        if node is None:
            return []
        if not node.is_folder:
            return [node] if node.state in ("ACTIVE", "TEMP") else []
        descendants = await self.repo.subtree(node_id, state=None)
        return [
            descendant
            for descendant in descendants
            if not descendant.is_folder and descendant.state in ("ACTIVE", "TEMP")
        ]

    # -- import / merge -----------------------------------------------------

    async def import_message(
        self,
        channel_id: int,
        message_id: int,
        *,
        parent_id: str = ROOT_ID,
        name: str | None = None,
        mime: str | None = None,
    ) -> Node | None:
        """Catalog a file posted directly to a channel (bot listener / watcher).

        Idempotent: a message already cataloged is skipped (dedupe by
        channel+message). A same-name ACTIVE node is left as-is; a DELETED one is
        resurrected in place (SAME node id, so old .strm URLs keep working).
        Returns None if the message carries no media.
        """
        already = await self.repo.get_file_by_message(channel_id, message_id)
        if already is not None:
            return already

        ref = await self._gateway.get_document(channel_id, message_id)
        if ref is None:
            return None  # not a file

        name = name or ref.filename or f"file_{message_id}"
        mime = mime or ref.mime or DEFAULT_MIME

        sibling = await self.repo.get_child_by_name(parent_id, name)
        if sibling is not None and sibling.state == "ACTIVE":
            return sibling
        if sibling is not None and sibling.state == "DELETED":
            await self.repo.clear_parts(sibling.id)
            await self.repo.add_part(
                sibling.id, idx=0, channel_id=channel_id, message_id=message_id,
                doc_id=ref.doc_id, size=ref.size, original_filename=name,
            )
            await self.repo.set_fields(
                sibling.id, state="ACTIVE", channel_id=channel_id, mime=mime, size=ref.size
            )
            await self.repo.session.commit()
            imported = await self.repo.get(sibling.id)
            if self._plugins_enabled():
                await self._run_after_plugin(
                    "after_file_import",
                    operation="file_import",
                    node=imported,
                    new_parent_id=parent_id,
                    new_path=await self.path_of(sibling.id),
                )
            return imported

        node = await self.repo.create(
            name=name, parent_id=parent_id, is_folder=False, mime=mime,
            channel_id=channel_id, state="ACTIVE", size=ref.size,
        )
        await self.repo.add_part(
            node.id, idx=0, channel_id=channel_id, message_id=message_id,
            doc_id=ref.doc_id, size=ref.size, original_filename=name,
        )
        await self.repo.session.commit()
        imported = await self.repo.get(node.id)
        if self._plugins_enabled():
            await self._run_after_plugin(
                "after_file_import",
                operation="file_import",
                node=imported,
                new_parent_id=parent_id,
                new_path=await self.path_of(node.id),
            )
        return imported

    async def _sync_part_captions(
        self, file_id: str, *, changed_fields: set[str] | None = None
    ) -> None:
        if self._caption_template == "":
            return
        if changed_fields is not None and not caption_template_depends_on(
            self._caption_template, changed_fields
        ):
            return
        node = await self.repo.get(file_id)
        if node is None or node.is_folder:
            return
        if await self.repo.content_of(file_id) is not None:
            return
        parts = list(await self.repo.parts_of(file_id))
        if not parts:
            return
        if self._gateway is None:
            raise ValueError("gateway unavailable: cannot sync Telegram captions")

        total = len(parts)
        parent_path = await self._parent_path(node)
        for pos, part in enumerate(parts):
            await self._gateway.edit_message_caption(
                part.channel_id,
                part.message_id,
                self._render_part_caption(
                    node, part, pos, total, parent_path=parent_path
                ),
            )

    async def _path_of_folder(self, folder_id: str | None) -> str:
        if folder_id is None:
            return "/"
        path = await self.repo.path_of(folder_id)
        return path or "/"

    async def _parent_path(self, node: Node) -> str:
        return await self._path_of_folder(node.parent_id)

    def _render_part_caption(
        self,
        node: Node,
        part: Any,
        idx: int,
        total_parts: int,
        *,
        parent_path: str,
        channel_id: int | None = None,
    ) -> str:
        return self._render_part_caption_values(
            node_id=node.id,
            parent_path=parent_path,
            logical_name=node.name,
            idx=idx,
            total_parts=total_parts,
            part=part,
            mime=node.mime,
            info_notes=_info_notes(node),
            channel_id=channel_id,
        )

    def _render_part_caption_values(
        self,
        *,
        node_id: str,
        parent_path: str,
        logical_name: str,
        idx: int,
        total_parts: int,
        part: Any,
        mime: str | None,
        info_notes: str = "",
        channel_id: int | None = None,
    ) -> str:
        return render_caption(
            self._caption_template,
            CaptionRenderContext(
                node_id=node_id,
                parent_path=parent_path,
                logical_name=logical_name,
                idx=idx,
                total_parts=total_parts,
                part_size=part.size,
                mime=mime,
                channel_id=part.channel_id if channel_id is None else channel_id,
                info_notes=info_notes,
            ),
        )

    async def merge_parts(
        self,
        target_id: str,
        donor_ids: Sequence[str],
        *,
        name: str | None = None,
        part_refs: Sequence[dict[str, int | str]] | None = None,
    ) -> Node:
        """Stitch chunked-upload files into one: append the donors' parts to the
        target, re-index 0..n (critical for streaming offsets), hard-delete the
        donor nodes (their messages are reassigned, not deleted). When part_refs
        is provided, it defines the exact output order as file_id/idx pairs."""
        all_ids = [target_id, *donor_ids]
        for nid in all_ids:
            if await self.repo.content_of(nid) is not None:
                raise ValueError(f"cannot merge inline (DB-stored) file {nid}")

        parts_by_ref: dict[tuple[str, int], Any] = {}
        for nid in all_ids:
            for part in await self.repo.parts_of(nid):
                parts_by_ref[(part.file_id, part.idx)] = part

        if not parts_by_ref:
            raise ValueError("cannot merge files with no parts")
        if part_refs is None:
            gathered = list(parts_by_ref.values())
        else:
            requested = [
                (str(ref.get("file_id")), int(ref.get("idx"))) for ref in part_refs
            ]
            expected = set(parts_by_ref)
            if set(requested) != expected or len(requested) != len(expected):
                raise ValueError("parts must reference each source part exactly once")
            gathered = [parts_by_ref[ref] for ref in requested]

        for nid in all_ids:
            await self.repo.clear_parts(nid)
        for i, p in enumerate(gathered):
            await self.repo.add_part(
                target_id, idx=i, channel_id=p.channel_id, message_id=p.message_id,
                doc_id=p.doc_id, size=p.size, original_filename=p.original_filename,
            )
        for donor_id in donor_ids:
            await self.repo.purge(donor_id)
        fields: dict[str, Any] = {"size": sum(p.size for p in gathered), "mtime": func.now()}
        if name is not None:
            target = await self.repo.get(target_id)
            if target is None or target.parent_id is None:
                raise NotAReadableFile(f"node {target_id} not found")
            fields["name"] = await self._available_child_name(
                target.parent_id, name, excluding=set(all_ids)
            )
        await self.repo.set_fields(target_id, **fields)
        await self.repo.session.commit()
        await self._sync_part_captions(target_id)
        return await self.repo.get(target_id)

    async def split_parts(self, file_id: str, part_indices: Sequence[int]) -> tuple[Node, list[Node]]:
        """Extract selected parts from one file into one-part sibling files.

        The source keeps the unselected parts. Extracted files use each part's
        original filename with active-sibling deduplication. Telegram messages are
        reassigned in metadata only; no copy/delete operation is performed.
        """
        if await self.repo.content_of(file_id) is not None:
            raise ValueError(f"cannot split an inline (DB-stored) file {file_id}")
        source = await self.repo.get(file_id)
        if source is None or source.is_folder or source.parent_id is None:
            raise NotAReadableFile(f"node {file_id} is not a splittable file")

        current = list(await self.repo.parts_of(file_id))
        selected = list(part_indices)
        if not selected:
            raise ValueError("part_indices must not be empty")
        if sorted(selected) != sorted(set(selected)):
            raise ValueError("part_indices must not contain duplicates")
        if any(i < 0 or i >= len(current) for i in selected):
            raise ValueError(f"part_indices must be within 0..{len(current) - 1}")
        if len(selected) == len(current):
            raise ValueError("cannot extract every part from the source file")

        selected_set = set(selected)
        kept = [p for pos, p in enumerate(current) if pos not in selected_set]
        extracted = [p for pos, p in enumerate(current) if pos in selected_set]

        await self.repo.clear_parts(file_id)
        for i, part in enumerate(kept):
            await self.repo.add_part(
                file_id, idx=i, channel_id=part.channel_id, message_id=part.message_id,
                doc_id=part.doc_id, size=part.size, original_filename=part.original_filename,
            )
        await self.repo.set_fields(
            file_id, size=sum(part.size for part in kept), mtime=func.now()
        )

        created: list[Node] = []
        for part in extracted:
            desired = part.original_filename or f"{source.name}.part-{part.idx}"
            name = await self._available_child_name(source.parent_id, desired)
            node = await self.repo.create(
                name=name, parent_id=source.parent_id, is_folder=False,
                mime=source.mime, channel_id=part.channel_id, state="ACTIVE",
                size=part.size,
            )
            await self.repo.add_part(
                node.id, idx=0, channel_id=part.channel_id, message_id=part.message_id,
                doc_id=part.doc_id, size=part.size, original_filename=part.original_filename,
            )
            created.append(node)

        await self.repo.session.commit()
        refreshed_source = await self.repo.get(file_id)
        refreshed_created = [await self.repo.get(node.id) for node in created]
        await self._sync_part_captions(file_id)
        created_nodes = [node for node in refreshed_created if node is not None]
        for node in created_nodes:
            await self._sync_part_captions(node.id)
        return refreshed_source, created_nodes

    async def reorder_parts(self, file_id: str, order: Sequence[int]) -> Node:
        """Re-sequence the parts of ONE multi-part file: `order` is a permutation
        of the current part indices (e.g. [2,0,1]) giving the new position order.
        Re-indexes 0..n in a single transaction. The total size is invariant under
        a permutation, but the BYTE LAYOUT served by /download changes — so mtime
        is bumped (the ETag folds it in) to invalidate caches. Raises ValueError on
        an inline file, no parts, or a non-permutation (which would drop/duplicate
        parts and change the size)."""
        if await self.repo.content_of(file_id) is not None:
            raise ValueError(f"cannot reorder parts of an inline (DB-stored) file {file_id}")
        current = list(await self.repo.parts_of(file_id))
        if not current:
            raise ValueError(f"file {file_id} has no parts to reorder")
        if sorted(order) != list(range(len(current))):
            raise ValueError(
                f"order must be a permutation of 0..{len(current) - 1} "
                f"(got {list(order)})"
            )

        reordered = [current[i] for i in order]
        await self.repo.clear_parts(file_id)
        for i, p in enumerate(reordered):
            await self.repo.add_part(
                file_id, idx=i, channel_id=p.channel_id, message_id=p.message_id,
                doc_id=p.doc_id, size=p.size, original_filename=p.original_filename,
            )
        # size unchanged by a permutation (re-asserted defensively); bump mtime so
        # the download ETag changes and clients/players don't serve stale bytes.
        await self.repo.set_fields(
            file_id, size=sum(p.size for p in reordered), mtime=func.now()
        )
        await self.repo.session.commit()
        await self._sync_part_captions(file_id)
        return await self.repo.get(file_id)

    # -- read ---------------------------------------------------------------

    async def open_read(
        self, node_id: str, start: int = 0, end: int | None = None
    ) -> AsyncIterator[bytes]:
        node = await self.repo.get(node_id)
        if node is None or node.is_folder or node.state != "ACTIVE":
            raise NotAReadableFile(f"node {node_id} is not a readable file")

        if end is None:
            end = node.size - 1

        content = await self.repo.content_of(node_id)
        if content is not None:
            total = len(content)
            if start < 0 or start > end or end >= total:
                raise RangeNotSatisfiable(f"range {start}-{end} of size {total}")
            yield content[start : end + 1]
            return

        parts = await self.repo.parts_of(node_id)
        plan = StreamPlan.build([p.size for p in parts], start, end)
        async for chunk in self._streamer.stream(parts, plan, node.channel_id):
            yield chunk


# -- per-file ops for the executor fan-out (run on the worker's fs) ---------

async def _reroute_op(fs: FileSystem, node_id: str) -> None:
    file = await fs.get(node_id)
    if file is not None and not file.is_folder:
        await fs._reroute_file(file)


async def _copy_op(fs: FileSystem, pair: tuple[str, str, bool]) -> str | None:
    src_id, dst_parent_id, force_copy = pair
    src = await fs.get(src_id)
    if src is None or src.is_folder:
        return None
    new = await fs._copy_file(src, dst_parent_id, force_copy=force_copy)
    return new.id


def _build_purge_file_plan(files: Sequence[Any], parts: Sequence[Any]) -> list[_PurgeFilePlan]:
    parts_by_file: dict[str, list[Any]] = {}
    for part in parts:
        file_id = getattr(part, "file_id", None)
        if file_id is None:
            continue
        parts_by_file.setdefault(str(file_id), []).append(part)

    plan: list[_PurgeFilePlan] = []
    for file in files:
        file_id = str(file.file_id)
        file_parts = tuple(parts_by_file.get(file_id, ()))
        if not file_parts:
            continue
        plan.append(
            _PurgeFilePlan(
                file_id=file_id,
                name=str(file.name),
                path=str(file.path),
                telegram_parts=int(getattr(file, "telegram_parts", len(file_parts))),
                channels=int(getattr(file, "channels", len({p.channel_id for p in file_parts}))),
                parts=file_parts,
            )
        )
    return plan


def _validate_purge_plan(parts: Sequence[Any], plan: Sequence[_PurgeFilePlan]) -> None:
    planned_parts = sum(len(file.parts) for file in plan)
    if planned_parts != len(parts):
        raise RuntimeError(
            "purge plan does not cover all Telegram parts: "
            f"planned={planned_parts} actual={len(parts)}"
        )


async def _purge_file_op(fs: FileSystem, file: _PurgeFilePlan) -> None:
    if fs._gateway is None:
        raise RuntimeError(f"no gateway available for purge file {file.path}")
    await _delete_purge_file_plan(fs._gateway, file)


async def _delete_purge_file_plans(
    gateway: Any, files: Sequence[_PurgeFilePlan]
) -> None:
    for file in files:
        await _delete_purge_file_plan(gateway, file)


async def _delete_purge_file_plan(gateway: Any, file: _PurgeFilePlan) -> None:
    log.info(
        "[purge] deleting file node=%s path=%s telegram_parts=%d",
        file.file_id,
        file.path,
        len(file.parts),
    )
    try:
        await _delete_purge_parts(gateway, file.parts)
    except Exception:
        log.exception(
            "[purge] failed deleting file node=%s path=%s telegram_parts=%d",
            file.file_id,
            file.path,
            len(file.parts),
        )
        raise


async def _delete_purge_parts(gateway: Any, parts: Sequence[PartRecord]) -> None:
    """Strict Telegram cleanup for purge.

    Move cleanup is best-effort because the moved copy is already durable. Purge
    is different: once DB rows are removed there is no reliable retry list for
    Telegram messages left behind.
    """
    for part in parts:
        try:
            await gateway.delete_message(part.channel_id, part.message_id)
        except Exception:
            log.exception(
                "[purge] failed deleting Telegram message %s in channel %s",
                part.message_id,
                part.channel_id,
            )
            raise


def _raise_first_error(results: list) -> None:
    for r in results:
        if isinstance(r, BaseException):
            raise r


def _first_result(results: list) -> Any:
    r = results[0]
    if isinstance(r, BaseException):
        raise r
    return r


def _log_failures(results: list, operation: str) -> None:
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        log.warning(
            "[%s] %d of %d item(s) failed (best-effort): %s",
            operation, len(errors), len(results), errors[0],
        )


def _path_depth(path: str) -> int:
    return 0 if not path else path.count("/") + 1
