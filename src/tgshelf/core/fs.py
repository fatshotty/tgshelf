"""FileSystem facade: the single API over the drive.

One surface for HTTP / CLI / bot / future mount, addressable by id or path,
nothing HTTP-shaped. It orchestrates NodeRepo (tree), the pools and the
download/upload/channels engines in transactions. This module is built up across
A8: reads + effective channel first, then write/open_read, tree ops, move/copy,
import/merge.
"""

from __future__ import annotations

from typing import Any, Sequence

from tgshelf.core import channels
from tgshelf.db.models import Node
from tgshelf.db.repo import NodeRepo


class FileSystem:
    def __init__(self, repo: NodeRepo, *, master_channel: int):
        self.repo = repo
        self._master_channel = master_channel

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

    # -- channel resolution -------------------------------------------------

    async def effective_channel(self, node_id: str, *, skip_current: bool = False) -> int:
        node = await self.repo.get(node_id)
        if node is None:
            return self._master_channel
        ancestors = await self.repo.ancestors(node_id)
        return channels.effective_channel(
            node, ancestors, self._master_channel, skip_current=skip_current
        )
