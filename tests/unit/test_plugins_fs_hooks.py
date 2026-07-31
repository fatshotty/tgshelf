from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from tgshelf.core.fs import FileSystem
from tgshelf.core.upload import UploadResult
from tgshelf.plugins import PluginError, PluginManager


@dataclass
class FakeNode:
    id: str
    name: str
    parent_id: str | None = None
    is_folder: bool = False
    state: str = "ACTIVE"
    size: int = 0
    channel_id: int | None = None
    mime: str | None = None
    info: dict[str, Any] | None = None
    content: bytes | None = None
    mtime: Any = None


@dataclass
class FakeDoc:
    filename: str
    mime: str
    doc_id: int
    size: int


class FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class FakeRepo:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.nodes: dict[str, FakeNode] = {
            "root": FakeNode("root", "", is_folder=True),
            "src": FakeNode("src", "src", parent_id="root", is_folder=True),
            "dst": FakeNode("dst", "dst", parent_id="root", is_folder=True),
            "rename": FakeNode(
                "rename", "Old.txt", parent_id="src", size=3, mime="text/plain", content=b"old"
            ),
            "move": FakeNode(
                "move", "Move.txt", parent_id="src", size=4, mime="text/plain", content=b"move"
            ),
            "copy": FakeNode(
                "copy", "Copy.txt", parent_id="src", size=4, mime="text/plain", content=b"copy"
            ),
            "delete": FakeNode(
                "delete", "Delete.txt", parent_id="src", size=6, mime="text/plain", content=b"delete"
            ),
            "season": FakeNode("season", "Season 01", parent_id="src", is_folder=True),
            "episode": FakeNode(
                "episode",
                "Episode.mkv",
                parent_id="season",
                size=7,
                mime="video/x-matroska",
                content=b"episode",
            ),
        }
        self.contents = {
            "rename": b"old",
            "move": b"move",
            "copy": b"copy",
            "delete": b"delete",
            "episode": b"episode",
        }
        self.parts: dict[str, list[Any]] = {}
        self.purged: list[str] = []

    async def get(self, node_id: str) -> FakeNode | None:
        return self.nodes.get(node_id)

    async def create(self, **fields: Any) -> FakeNode:
        node_id = f"created-{len([key for key in self.nodes if key.startswith('created-')]) + 1}"
        content = fields.pop("content", None)
        node = FakeNode(id=node_id, content=content, **fields)
        self.nodes[node_id] = node
        if content is not None:
            self.contents[node_id] = content
        return node

    async def set_fields(self, node_id: str, **fields: Any) -> None:
        node = self.nodes[node_id]
        self.nodes[node_id] = replace(node, **fields)
        if "content" in fields:
            if fields["content"] is None:
                self.contents.pop(node_id, None)
            else:
                self.contents[node_id] = fields["content"]

    async def set_state_subtree(
        self, node_id: str, state: str, *, from_states: tuple[str, ...]
    ) -> None:
        ids = [node_id] + [node.id for node in await self.subtree(node_id, state=None)]
        for current_id in ids:
            node = self.nodes.get(current_id)
            if node is not None and node.state in from_states:
                self.nodes[current_id] = replace(node, state=state)

    async def purge(self, node_id: str) -> None:
        self.purged.append(node_id)
        self.nodes.pop(node_id, None)
        self.contents.pop(node_id, None)
        self.parts.pop(node_id, None)

    async def children(
        self,
        parent_id: str,
        *,
        state: str | None = "ACTIVE",
        folders_only: bool = False,
        files_only: bool = False,
    ) -> list[FakeNode]:
        children = [node for node in self.nodes.values() if node.parent_id == parent_id]
        if state is not None:
            children = [node for node in children if node.state == state]
        if folders_only:
            children = [node for node in children if node.is_folder]
        if files_only:
            children = [node for node in children if not node.is_folder]
        return children

    async def subtree(self, node_id: str, *, state: str | None = "ACTIVE") -> list[FakeNode]:
        result = []
        stack = [node_id]
        while stack:
            parent_id = stack.pop()
            for node in self.nodes.values():
                if node.parent_id != parent_id:
                    continue
                if state is None or node.state == state:
                    result.append(node)
                if node.is_folder:
                    stack.append(node.id)
        return result

    async def ancestors(self, node_id: str) -> list[FakeNode]:
        ancestors = []
        node = self.nodes.get(node_id)
        while node is not None and node.parent_id is not None:
            parent = self.nodes[node.parent_id]
            ancestors.append(parent)
            node = parent
        return list(reversed(ancestors))

    async def get_child_by_name(
        self, parent_id: str, name: str, *, state: str | None = None
    ) -> FakeNode | None:
        for node in self.nodes.values():
            if node.parent_id != parent_id or node.name.lower() != name.lower():
                continue
            if state is None or node.state == state:
                return node
        return None

    async def path_of(self, node_id: str) -> str | None:
        node = self.nodes.get(node_id)
        if node is None:
            return None
        segments = []
        while node.parent_id is not None:
            segments.append(node.name)
            node = self.nodes[node.parent_id]
        return "/" + "/".join(reversed(segments))

    async def content_of(self, node_id: str) -> bytes | None:
        return self.contents.get(node_id)

    async def parts_of(self, node_id: str) -> list[Any]:
        return self.parts.get(node_id, [])

    async def parts_size(self, node_id: str) -> int:
        return sum(part.size for part in self.parts.get(node_id, []))

    async def clear_parts(self, node_id: str) -> None:
        self.parts[node_id] = []

    async def add_part(self, file_id: str, **fields: Any) -> None:
        self.parts.setdefault(file_id, []).append(type("Part", (), {"file_id": file_id, **fields})())

    async def get_file_by_message(self, channel_id: int, message_id: int) -> FakeNode | None:
        for file_id, parts in self.parts.items():
            if any(part.channel_id == channel_id and part.message_id == message_id for part in parts):
                return self.nodes[file_id]
        return None


class BlockingUploadPlugin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def before_file_upload(self, ctx: Any) -> None:
        self.calls.append((ctx.operation, ctx.node.state, ctx.new_path))
        raise PluginError("blocked upload")


class RecordingPlugin:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def before_file_rename(self, ctx: Any) -> None:
        self.calls.append(("before_file_rename", ctx.node.id, ctx.old_path, ctx.new_path))

    async def after_file_rename(self, ctx: Any) -> None:
        self.calls.append(("after_file_rename", ctx.node.id, ctx.old_path, ctx.new_path))

    async def before_file_move(self, ctx: Any) -> None:
        self.calls.append(("before_file_move", ctx.node.id, ctx.old_path, ctx.new_path))

    async def after_file_move(self, ctx: Any) -> None:
        self.calls.append(("after_file_move", ctx.node.id, ctx.old_path, ctx.new_path))

    async def before_file_copy(self, ctx: Any) -> None:
        self.calls.append(("before_file_copy", ctx.node.id, ctx.old_path, ctx.new_path))

    async def after_file_copy(self, ctx: Any) -> None:
        self.calls.append(
            (
                "after_file_copy",
                ctx.node.id,
                ctx.source_node.id,
                ctx.old_path,
                ctx.new_path,
            )
        )

    async def before_file_delete(self, ctx: Any) -> None:
        self.calls.append(("before_file_delete", ctx.node.id, ctx.old_path))

    async def after_file_delete(self, ctx: Any) -> None:
        self.calls.append(("after_file_delete", ctx.node.id, ctx.node.state, ctx.old_path))

    async def after_file_import(self, ctx: Any) -> None:
        self.calls.append(("after_file_import", ctx.node.id, ctx.new_path))


class InlineUploader:
    def __init__(self) -> None:
        self.called = False

    async def upload(self, *args: Any, **kwargs: Any) -> UploadResult:
        self.called = True
        return UploadResult(size=6, inline_content=b"upload")


class FakeGateway:
    async def get_document(self, channel_id: int, message_id: int) -> FakeDoc:
        return FakeDoc(
            filename="Imported.bin",
            mime="application/octet-stream",
            doc_id=123,
            size=7,
        )


async def chunks():
    yield b"upload"


@pytest.mark.asyncio
async def test_before_file_upload_can_block_and_removes_temp_node() -> None:
    repo = FakeRepo()
    uploader = InlineUploader()
    plugin = BlockingUploadPlugin()
    fs = FileSystem(
        repo,
        master_channel=-100,
        uploader=uploader,
        plugin_manager=PluginManager([plugin]),
    )

    with pytest.raises(PluginError, match="blocked upload"):
        await fs.write("src", "Upload.txt", chunks)

    assert uploader.called is False
    assert repo.purged == ["created-1"]
    assert plugin.calls == [("file_upload", "TEMP", "/src/Upload.txt")]


@pytest.mark.asyncio
async def test_file_operation_hooks_receive_logical_contexts() -> None:
    repo = FakeRepo()
    plugin = RecordingPlugin()
    fs = FileSystem(
        repo,
        master_channel=-100,
        gateway=FakeGateway(),
        plugin_manager=PluginManager([plugin]),
    )

    await fs.rename("rename", "New.txt")
    await fs.move("move", "dst")
    copied = await fs.copy("copy", "dst", force_copy=True)
    await fs.delete("delete")
    imported = await fs.import_message(-100, 77, parent_id="dst")

    assert plugin.calls == [
        ("before_file_rename", "rename", "/src/Old.txt", "/src/New.txt"),
        ("after_file_rename", "rename", "/src/Old.txt", "/src/New.txt"),
        ("before_file_move", "move", "/src/Move.txt", "/dst/Move.txt"),
        ("after_file_move", "move", "/src/Move.txt", "/dst/Move.txt"),
        ("before_file_copy", "copy", "/src/Copy.txt", "/dst/Copy.txt"),
        ("after_file_copy", copied.id, "copy", "/src/Copy.txt", f"/dst/{copied.name}"),
        ("before_file_delete", "delete", "/src/Delete.txt"),
        ("after_file_delete", "delete", "DELETED", "/src/Delete.txt"),
        ("after_file_import", imported.id, "/dst/Imported.bin"),
    ]


@pytest.mark.asyncio
async def test_folder_move_runs_file_move_hooks_for_descendant_files() -> None:
    repo = FakeRepo()
    plugin = RecordingPlugin()
    fs = FileSystem(
        repo,
        master_channel=-100,
        plugin_manager=PluginManager([plugin]),
    )

    await fs.move("season", "dst")

    assert plugin.calls == [
        ("before_file_move", "episode", "/src/Season 01/Episode.mkv", "/dst/Season 01/Episode.mkv"),
        ("after_file_move", "episode", "/src/Season 01/Episode.mkv", "/dst/Season 01/Episode.mkv"),
    ]


@pytest.mark.asyncio
async def test_move_folder_into_its_descendant_fails_before_mutation_or_hooks() -> None:
    repo = FakeRepo()
    plugin = RecordingPlugin()
    fs = FileSystem(
        repo,
        master_channel=-100,
        plugin_manager=PluginManager([plugin]),
    )
    original_subtree = repo.subtree

    async def fail_if_cycle_created(
        node_id: str, *, state: str | None = "ACTIVE"
    ) -> list[FakeNode]:
        if repo.nodes["src"].parent_id == "season":
            raise RuntimeError("cycle created before subtree traversal")
        return await original_subtree(node_id, state=state)

    repo.subtree = fail_if_cycle_created  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="cannot move a folder into itself or its descendant"):
        await fs.move("src", "season")

    assert repo.nodes["src"].parent_id == "root"
    assert plugin.calls == []
