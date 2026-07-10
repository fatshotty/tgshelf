from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

import pytest

from tgshelf.core.fs import FileSystem
from tgshelf.core.upload import UploadResult
from tgshelf.plugins import PluginManager


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


class FakeSession:
    async def commit(self) -> None:
        pass


class FakeRepo:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.nodes: dict[str, FakeNode] = {
            "root": FakeNode("root", "", is_folder=True),
            "media": FakeNode("media", "media", parent_id="root", is_folder=True),
            "movies": FakeNode("movies", "movies", parent_id="media", is_folder=True),
            "show": FakeNode(
                "show",
                "XXX (2026)",
                parent_id="movies",
                is_folder=True,
            ),
            "season": FakeNode(
                "season",
                "Season 01",
                parent_id="show",
                is_folder=True,
            ),
            "nfo": FakeNode(
                "nfo",
                "tvshow.nfo",
                parent_id="show",
                size=len(b"<tmdbid>33333</tmdbid>"),
                mime="text/xml",
            ),
        }
        self.contents = {"nfo": b"<tmdbid>33333</tmdbid>"}

    async def get(self, node_id: str) -> FakeNode | None:
        return self.nodes.get(node_id)

    async def create(self, **fields: Any) -> FakeNode:
        node_id = f"created-{len(self.nodes)}"
        node = FakeNode(id=node_id, **fields)
        self.nodes[node_id] = node
        return node

    async def set_fields(self, node_id: str, **fields: Any) -> None:
        node = self.nodes[node_id]
        self.nodes[node_id] = replace(node, **fields)
        if "content" in fields:
            self.contents[node_id] = fields["content"]

    async def purge(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)
        self.contents.pop(node_id, None)

    async def ancestors(self, node_id: str) -> list[FakeNode]:
        ancestors = []
        node = self.nodes.get(node_id)
        while node is not None and node.parent_id is not None:
            parent = self.nodes[node.parent_id]
            ancestors.append(parent)
            node = parent
        return list(reversed(ancestors))

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

    async def get_child_by_name(
        self, parent_id: str, name: str, *, state: str | None = None
    ) -> FakeNode | None:
        for node in self.nodes.values():
            if node.parent_id != parent_id or node.name.lower() != name.lower():
                continue
            if state is None or node.state == state:
                return node
        return None

    async def content_of(self, node_id: str) -> bytes | None:
        return self.contents.get(node_id)

    async def parts_size(self, node_id: str) -> int:
        return 0

    async def parts_of(self, node_id: str) -> list[Any]:
        return []

    async def path_of(self, node_id: str) -> str | None:
        node = self.nodes.get(node_id)
        if node is None:
            return None
        segments = []
        while node.parent_id is not None:
            segments.append(node.name)
            node = self.nodes[node.parent_id]
        return "/" + "/".join(reversed(segments))


class InlineUploader:
    async def upload(self, *args: Any, **kwargs: Any) -> UploadResult:
        return UploadResult(size=5, inline_content=b"video")


class TmdbNotesPlugin:
    async def after_file_upload(self, ctx: Any) -> None:
        season = await ctx.host.parent(ctx.node)
        show = await ctx.host.parent(season)
        nfo = await ctx.host.get_child_by_name(show.id, "tvshow.nfo")
        content = await ctx.host.read_text(nfo.id)
        tmdb_id = re.search(r"<tmdbid>(\d+)</tmdbid>", content).group(1)

        current_notes = await ctx.host.get_info_notes(ctx.node.id)
        lines = [line for line in current_notes.splitlines() if not line.startswith("TMDB:")]
        lines.append(f"TMDB: {tmdb_id}")
        await ctx.host.set_info_notes(ctx.node.id, "\n".join(lines))


async def chunks():
    yield b"video"


@pytest.mark.asyncio
async def test_write_runs_after_upload_plugins_on_logical_file_node() -> None:
    repo = FakeRepo()
    fs = FileSystem(
        repo,
        master_channel=-100,
        uploader=InlineUploader(),
        min_size=100,
        plugin_manager=PluginManager([TmdbNotesPlugin()]),
    )

    uploaded = await fs.write(
        "season",
        "XXX (2026) - S01E01 - Episode 1.mkv",
        chunks,
    )

    assert uploaded.info == {"notes": "TMDB: 33333"}
