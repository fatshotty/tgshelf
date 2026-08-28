"""Planning primitives for virtual-to-virtual folder mirrors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MirrorAction:
    kind: str
    path: str
    source_id: str | None = None
    dest_id: str | None = None
    dest_is_folder: bool | None = None


@dataclass
class MirrorPlan:
    source_id: str
    dest_id: str
    actions: list[MirrorAction]

    def count(self, kind: str) -> int:
        return sum(1 for action in self.actions if action.kind == kind)


@dataclass(frozen=True)
class MirrorFailure:
    action: MirrorAction
    error: str


@dataclass(frozen=True)
class MirrorRun:
    plan: MirrorPlan
    created: int
    copied: int
    replaced: int
    deleted: int
    skipped: int
    deferred: int
    completed: bool
    failures: tuple[MirrorFailure, ...]

    @property
    def actions(self) -> list[MirrorAction]:
        """Compatibility view of the planned actions."""
        return self.plan.actions


async def build_mirror_plan(repo: Any, source: Any, dest: Any) -> MirrorPlan:
    source_entries = _entries_by_key(source, await repo.subtree(source.id, state="ACTIVE"))
    dest_entries = _entries_by_key(dest, await repo.subtree(dest.id, state="ACTIVE"))
    actions: list[MirrorAction] = []
    replaced_folder_keys: set[tuple[str, ...]] = set()

    for key in sorted(source_entries):
        path, source_node = source_entries[key]
        dest_entry = dest_entries.get(key)
        if dest_entry is None:
            actions.append(
                MirrorAction(
                    "create_folder" if source_node.is_folder else "copy_file",
                    path,
                    source_id=source_node.id,
                )
            )
            continue

        _dest_path, dest_node = dest_entry
        if source_node.is_folder and not dest_node.is_folder:
            actions.append(
                MirrorAction(
                    "replace_file_with_folder",
                    path,
                    source_id=source_node.id,
                    dest_id=dest_node.id,
                    dest_is_folder=False,
                )
            )
        elif not source_node.is_folder and dest_node.is_folder:
            replaced_folder_keys.add(key)
            actions.append(
                MirrorAction(
                    "replace_folder_with_file",
                    path,
                    source_id=source_node.id,
                    dest_id=dest_node.id,
                    dest_is_folder=True,
                )
            )
        elif source_node.is_folder:
            actions.append(
                MirrorAction("skip", path, source_id=source_node.id, dest_id=dest_node.id)
            )
        elif await _same_file(repo, source_node, dest_node):
            actions.append(
                MirrorAction("skip", path, source_id=source_node.id, dest_id=dest_node.id)
            )
        else:
            actions.append(
                MirrorAction(
                    "replace_file",
                    path,
                    source_id=source_node.id,
                    dest_id=dest_node.id,
                    dest_is_folder=False,
                )
            )

    extra_folder_keys: set[tuple[str, ...]] = set()
    for key in sorted(dest_entries):
        if key in source_entries:
            continue
        path, dest_node = dest_entries[key]
        if any(
            key[:depth] in extra_folder_keys or key[:depth] in replaced_folder_keys
            for depth in range(1, len(key))
        ):
            continue
        actions.append(
            MirrorAction(
                "delete_extra",
                path,
                dest_id=dest_node.id,
                dest_is_folder=dest_node.is_folder,
            )
        )
        if dest_node.is_folder:
            extra_folder_keys.add(key)

    return MirrorPlan(source_id=source.id, dest_id=dest.id, actions=_sort_actions(actions))


def _entries_by_key(root: Any, descendants: list[Any]) -> dict[tuple[str, ...], tuple[str, Any]]:
    nodes = {root.id: root}
    nodes.update({node.id: node for node in descendants})
    entries: dict[tuple[str, ...], tuple[str, Any]] = {}
    for node in descendants:
        parts: list[str] = []
        current = node
        while current.id != root.id:
            parts.append(current.name)
            current = nodes[current.parent_id]
        names = tuple(reversed(parts))
        entries[tuple(name.lower() for name in names)] = ("/".join(names), node)
    return entries


async def _same_file(repo: Any, source: Any, dest: Any) -> bool:
    if source.size != dest.size:
        return False

    source_content = await repo.content_of(source.id)
    dest_content = await repo.content_of(dest.id)
    if source_content is not None or dest_content is not None:
        return (
            source_content is not None
            and dest_content is not None
            and source_content == dest_content
        )

    source_parts = list(await repo.parts_of(source.id))
    dest_parts = list(await repo.parts_of(dest.id))
    if not source_parts or not dest_parts or len(source_parts) != len(dest_parts):
        return False

    source_fingerprint = [_part_fingerprint(part) for part in source_parts]
    dest_fingerprint = [_part_fingerprint(part) for part in dest_parts]
    if any(part is None for part in source_fingerprint + dest_fingerprint):
        return False
    return source_fingerprint == dest_fingerprint


def _part_fingerprint(part: Any) -> tuple[int, int, int] | None:
    doc_id = getattr(part, "doc_id", None)
    if doc_id is None:
        return None
    return (int(part.idx), int(part.size), int(doc_id))


def _sort_actions(actions: list[MirrorAction]) -> list[MirrorAction]:
    priority = {
        "replace_file_with_folder": 10,
        "create_folder": 20,
        "copy_file": 30,
        "replace_file": 30,
        "replace_folder_with_file": 30,
        "delete_extra": 40,
        "skip": 50,
    }
    return sorted(
        actions,
        key=lambda action: (priority[action.kind], action.path.lower()),
    )
