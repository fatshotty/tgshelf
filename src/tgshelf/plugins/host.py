"""Stable host facade exposed to trusted plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class PluginNode:
    id: str
    name: str
    parent_id: str | None
    is_folder: bool
    mime: str | None
    size: int
    state: str
    info: Mapping[str, Any]
    path: str | None = None


class PluginHost:
    def __init__(self, fs: Any):
        self._fs = fs

    async def get_node(self, node_id: str) -> PluginNode | None:
        return await self._plugin_node(await self._fs.get(node_id))

    async def parent(self, node_or_id: PluginNode | str) -> PluginNode | None:
        node = node_or_id
        if isinstance(node_or_id, str):
            node = await self.get_node(node_or_id)
        if node is None or node.parent_id is None:
            return None
        return await self.get_node(node.parent_id)

    async def ancestors(self, node_id: str) -> list[PluginNode]:
        nodes = await self._fs.repo.ancestors(node_id)
        result = []
        for node in nodes:
            plugin_node = await self._plugin_node(node)
            if plugin_node is not None:
                result.append(plugin_node)
        return result

    async def path_of(self, node_id: str) -> str | None:
        return await self._fs.path_of(node_id)

    async def list_children(self, folder_id: str) -> list[PluginNode]:
        children = await self._fs.list_children(folder_id)
        result = []
        for child in children:
            node = await self._plugin_node(child)
            if node is not None:
                result.append(node)
        return result

    async def get_child_by_name(
        self, parent_id: str, name: str
    ) -> PluginNode | None:
        node = await self._fs.repo.get_child_by_name(parent_id, name, state="ACTIVE")
        return await self._plugin_node(node)

    async def read_text(self, node_id: str, *, max_bytes: int = 1_048_576) -> str:
        node = await self._fs.get(node_id)
        if node is None or node.is_folder or node.state != "ACTIVE":
            raise ValueError(f"node {node_id} is not a readable file")
        if max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")
        if node.size > max_bytes:
            raise ValueError(f"node {node_id} exceeds read_text limit ({max_bytes} bytes)")
        chunks: list[bytes] = []
        total = 0
        async for chunk in self._fs.open_read(node_id):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(
                    f"node {node_id} exceeds read_text limit ({max_bytes} bytes)"
                )
            chunks.append(chunk)
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"node {node_id} is not valid UTF-8 text") from exc

    async def get_info(self, node_id: str) -> dict[str, Any]:
        node = await self._fs.get(node_id)
        if node is None:
            raise ValueError(f"node {node_id} not found")
        return dict(node.info or {})

    async def get_info_notes(self, node_id: str) -> str:
        info = await self.get_info(node_id)
        notes = info.get("notes", "")
        return notes if isinstance(notes, str) else ""

    async def update_info(self, node_id: str, patch: Mapping[str, Any]) -> PluginNode:
        if "notes" in patch:
            raise ValueError("use set_info_notes() to update info.notes")
        node = await self._fs.get(node_id)
        if node is None:
            raise ValueError(f"node {node_id} not found")
        info = dict(node.info or {})
        info.update(dict(patch))
        await self._fs.repo.set_fields(node_id, info=info)
        await self._fs.repo.session.commit()
        updated = await self.get_node(node_id)
        if updated is None:
            raise ValueError(f"node {node_id} not found after update")
        return updated

    async def set_info_notes(self, node_id: str, notes: str) -> PluginNode:
        updated = await self._fs.set_info_notes(node_id, notes)
        plugin_node = await self._plugin_node(updated)
        if plugin_node is None:
            raise ValueError(f"node {node_id} not found after notes update")
        return plugin_node

    async def resync_caption(self, node_id: str) -> None:
        await self._fs.resync_caption(node_id)

    async def _plugin_node(self, node: Any | None) -> PluginNode | None:
        if node is None:
            return None
        return PluginNode(
            id=node.id,
            name=node.name,
            parent_id=node.parent_id,
            is_folder=node.is_folder,
            mime=node.mime,
            size=node.size,
            state=node.state,
            info=dict(node.info or {}),
            path=await self._fs.path_of(node.id),
        )
