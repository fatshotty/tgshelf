from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, AsyncIterator

import pytest

from tgshelf.plugins import PluginHost


@dataclass(frozen=True)
class FakeNode:
    id: str
    name: str
    parent_id: str | None
    is_folder: bool
    mime: str | None = None
    size: int = 0
    state: str = "ACTIVE"
    info: dict[str, Any] | None = None


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeRepo:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.nodes = {
            "root": FakeNode("root", "", None, True, info={}),
            "media": FakeNode("media", "media", "root", True, info={}),
            "movies": FakeNode("movies", "movies", "media", True, info={}),
            "show": FakeNode("show", "XXX (2026)", "movies", True, info={}),
            "season": FakeNode("season", "Season 01", "show", True, info={}),
            "episode": FakeNode(
                "episode",
                "XXX (2026) - S01E01 - Episode 1.mkv",
                "season",
                False,
                mime="video/x-matroska",
                size=123,
                info={"notes": "personal note"},
            ),
            "nfo": FakeNode(
                "nfo",
                "tvshow.nfo",
                "show",
                False,
                mime="text/xml",
                size=30,
                info={},
            ),
        }

    async def get(self, node_id: str) -> FakeNode | None:
        return self.nodes.get(node_id)

    async def ancestors(self, node_id: str) -> list[FakeNode]:
        node = self.nodes[node_id]
        result = []
        parent_id = node.parent_id
        while parent_id is not None:
            parent = self.nodes[parent_id]
            result.append(parent)
            parent_id = parent.parent_id
        return list(reversed(result))

    async def children(
        self,
        parent_id: str,
        *,
        state: str | None = "ACTIVE",
        folders_only: bool = False,
        files_only: bool = False,
    ) -> list[FakeNode]:
        nodes = [
            node
            for node in self.nodes.values()
            if node.parent_id == parent_id and (state is None or node.state == state)
        ]
        if folders_only:
            nodes = [node for node in nodes if node.is_folder]
        if files_only:
            nodes = [node for node in nodes if not node.is_folder]
        return nodes

    async def get_child_by_name(
        self, parent_id: str, name: str, *, state: str | None = None
    ) -> FakeNode | None:
        for node in await self.children(parent_id, state=state):
            if node.name.lower() == name.lower():
                return node
        return None

    async def set_fields(self, node_id: str, **fields: Any) -> None:
        self.nodes[node_id] = replace(self.nodes[node_id], **fields)


class FakeFileSystem:
    def __init__(self) -> None:
        self.repo = FakeRepo()
        self.contents = {"nfo": b"<tvshow><tmdbid>33333</tmdbid></tvshow>"}
        self.synced: list[str] = []

    async def get(self, node_id: str) -> FakeNode | None:
        return await self.repo.get(node_id)

    async def path_of(self, node_id: str) -> str | None:
        node = await self.repo.get(node_id)
        if node is None:
            return None
        parts = []
        while node.parent_id is not None:
            parts.append(node.name)
            node = await self.repo.get(node.parent_id)
        return "/" + "/".join(reversed(parts))

    async def list_children(self, node_id: str):
        return await self.repo.children(node_id)

    async def open_read(self, node_id: str) -> AsyncIterator[bytes]:
        yield self.contents[node_id]

    async def set_info_notes(self, node_id: str, notes: str):
        node = await self.repo.get(node_id)
        info = dict(node.info or {})
        info["notes"] = notes
        await self.repo.set_fields(node_id, info=info)
        await self.repo.session.commit()
        return await self.repo.get(node_id)

    async def resync_caption(self, node_id: str) -> None:
        self.synced.append(node_id)


@pytest.mark.asyncio
async def test_plugin_host_navigates_tree_and_reads_text() -> None:
    fs = FakeFileSystem()
    host = PluginHost(fs)

    episode = await host.get_node("episode")
    ancestors = await host.ancestors(episode.id)
    show_folder = ancestors[-2]
    nfo = await host.get_child_by_name(show_folder.id, "tvshow.nfo")
    text = await host.read_text(nfo.id)

    assert episode.path == "/media/movies/XXX (2026)/Season 01/XXX (2026) - S01E01 - Episode 1.mkv"
    assert [node.path for node in ancestors] == [
        "/",
        "/media",
        "/media/movies",
        "/media/movies/XXX (2026)",
        "/media/movies/XXX (2026)/Season 01",
    ]
    assert nfo.path == "/media/movies/XXX (2026)/tvshow.nfo"
    assert "33333" in text


@pytest.mark.asyncio
async def test_plugin_host_reads_and_replaces_multiline_notes() -> None:
    fs = FakeFileSystem()
    host = PluginHost(fs)

    notes = await host.get_info_notes("episode")
    updated = await host.set_info_notes("episode", notes + "\nTMDB: 33333")

    assert updated.info["notes"] == "personal note\nTMDB: 33333"
    assert await host.get_info_notes("episode") == "personal note\nTMDB: 33333"


@pytest.mark.asyncio
async def test_plugin_host_update_info_rejects_notes_patch() -> None:
    fs = FakeFileSystem()
    host = PluginHost(fs)

    with pytest.raises(ValueError, match="set_info_notes"):
        await host.update_info("episode", {"notes": "bypass"})


@pytest.mark.asyncio
async def test_plugin_host_read_text_enforces_max_bytes() -> None:
    fs = FakeFileSystem()
    host = PluginHost(fs)

    with pytest.raises(ValueError, match="exceeds"):
        await host.read_text("nfo", max_bytes=8)
