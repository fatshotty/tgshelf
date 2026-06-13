"""FileSystem facade: the single API over the drive.

One surface for HTTP / CLI / bot / future mount, addressable by id or path,
nothing HTTP-shaped. It orchestrates NodeRepo (tree), the pools and the
download/upload/channels engines in transactions. This module is built up across
A8: reads + effective channel first, then write/open_read, tree ops, move/copy,
import/merge.
"""

from __future__ import annotations

import mimetypes
from typing import Any, AsyncIterator, Callable, Sequence

from tgshelf.core import channels
from tgshelf.core.download import RangeNotSatisfiable, StreamPlan
from tgshelf.core.upload import PartRecord
from tgshelf.db.models import Node
from tgshelf.db.repo import NodeRepo

DEFAULT_MIME = "application/octet-stream"


class NotAReadableFile(Exception):
    """The node is missing, a folder, or not ACTIVE."""


class FileSystem:
    def __init__(
        self,
        repo: NodeRepo,
        *,
        master_channel: int,
        uploader: Any = None,
        streamer: Any = None,
        min_size: int = 0,
    ):
        self.repo = repo
        self._master_channel = master_channel
        self._uploader = uploader
        self._streamer = streamer
        self._min_size = min_size

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

    # -- write --------------------------------------------------------------

    async def write(
        self,
        parent_id: str,
        name: str,
        source_factory: Callable[[], AsyncIterator[bytes]],
        *,
        mime: str | None = None,
    ) -> Node:
        """Upload a file under `parent_id`, converging the three upload paths
        (CLI sync / mount PUT / webui) into one place.

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
            )
        except BaseException:
            await self.repo.purge(node.id)  # drop TEMP (parts cascade)
            await self.repo.session.commit()
            raise

        if result.inline_content is not None:
            await self.repo.set_fields(
                node.id, content=result.inline_content, size=result.size, state="ACTIVE"
            )
        else:
            await self.repo.set_fields(node.id, size=result.size, state="ACTIVE")
        await self.repo.session.commit()
        return await self.repo.get(node.id)

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
