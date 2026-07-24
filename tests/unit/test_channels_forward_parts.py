from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from tgshelf.core.captions import (
    DEFAULT_CAPTION_TEMPLATE,
    CaptionRenderContext,
    logical_part_caption,
    logical_part_filename,
    render_caption,
)
from tgshelf.core.channels import forward_parts
from tgshelf.core.fs import FileSystem
from tgshelf.core.upload import PartRecord, UploadResult
from tgshelf.telegram.gateway import DocRef


class FakeGateway:
    def __init__(self):
        self.copies = []

    async def get_document(self, channel_id: int, message_id: int) -> DocRef | None:
        return DocRef(location=None, doc_id=42, dc_id=1, size=123)

    async def copy_message(
        self,
        from_channel_id: int,
        message_id: int,
        to_channel_id: int,
        *,
        caption: str | None = None,
    ) -> tuple[int, int]:
        self.copies.append((from_channel_id, message_id, to_channel_id, caption))
        return 77, 42


@pytest.mark.asyncio
async def test_forward_parts_can_override_caption_per_part():
    gateway = FakeGateway()
    part = PartRecord(
        idx=0,
        channel_id=-100,
        message_id=7,
        doc_id=42,
        size=123,
        original_filename="source.mkv",
    )

    await forward_parts(
        gateway,
        [part],
        -200,
        caption_factory=lambda p: f"fileName: restored-{p.idx}.mkv",
    )

    assert gateway.copies == [(-100, 7, -200, "fileName: restored-0.mkv")]


def test_single_part_caption_uses_logical_name_without_suffix():
    assert logical_part_filename("Movie.mkv", idx=0, total_parts=1) == "Movie.mkv"
    assert logical_part_caption("Movie.mkv", idx=0, total_parts=1) == "fileName: Movie.mkv"


def test_multi_part_caption_uses_one_based_three_digit_suffix():
    assert logical_part_filename("Movie.mkv", idx=0, total_parts=3) == "Movie.mkv.001"
    assert logical_part_filename("Movie.mkv", idx=1, total_parts=3) == "Movie.mkv.002"
    assert logical_part_caption("Movie.mkv", idx=2, total_parts=3) == "fileName: Movie.mkv.003"


def test_caption_template_renders_parent_path_and_part_metadata():
    caption = render_caption(
        "{path}\nfileName: {filename}\npart: {part_idx}/{parts}\nsize: {size}\n{id}\n{mime}\n{channel_id}\n{info}",
        CaptionRenderContext(
            node_id="abc123def4",
            parent_path="/backup/movies-bk-1",
            logical_name="Inception.mkv",
            idx=0,
            total_parts=2,
            part_size=2097152,
            mime="video/x-matroska",
            channel_id=-100123,
            info_notes="Director cut",
        ),
    )

    assert caption == (
        "/backup/movies-bk-1\n"
        "fileName: Inception.mkv.001\n"
        "part: 1/2\n"
        "size: 2097152\n"
        "abc123def4\n"
        "video/x-matroska\n"
        "-100123\n"
        "Director cut"
    )


def test_empty_caption_template_renders_empty_caption():
    caption = render_caption(
        "",
        CaptionRenderContext(
            node_id="abc123def4",
            parent_path="/",
            logical_name="Movie.mkv",
            idx=0,
            total_parts=1,
            part_size=123,
            mime="video/x-matroska",
            channel_id=-100,
        ),
    )

    assert caption == ""


@dataclass
class FakeNode:
    id: str
    name: str
    parent_id: str | None = "root"
    is_folder: bool = False
    state: str = "ACTIVE"
    size: int = 0
    channel_id: int | None = -100
    mime: str | None = "video/x-matroska"
    info: dict[str, Any] | None = None
    mtime: Any = None


@dataclass
class FakePart:
    file_id: str
    idx: int
    channel_id: int
    message_id: int
    doc_id: int | None
    size: int
    original_filename: str | None


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


class FakeRepo:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.nodes: dict[str, FakeNode] = {}
        self.parts: dict[str, list[FakePart]] = {}
        self.purged: list[str] = []

    async def get(self, node_id: str) -> FakeNode | None:
        return self.nodes.get(node_id)

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

    async def content_of(self, node_id: str) -> bytes | None:
        return None

    async def parts_of(self, file_id: str) -> list[FakePart]:
        return sorted(self.parts.get(file_id, []), key=lambda part: part.idx)

    async def parts_size(self, file_id: str) -> int:
        return sum(part.size for part in self.parts.get(file_id, []))

    async def path_of(self, node_id: str) -> str | None:
        node = self.nodes.get(node_id)
        if node is None:
            return None
        segments = []
        current = node
        while current.parent_id is not None:
            segments.append(current.name)
            current = self.nodes[current.parent_id]
        return "/" + "/".join(reversed(segments))

    async def clear_parts(self, file_id: str) -> None:
        self.parts[file_id] = []

    async def add_part(
        self,
        file_id: str,
        *,
        idx: int,
        channel_id: int,
        message_id: int,
        doc_id: int | None,
        size: int,
        original_filename: str | None,
    ) -> None:
        self.parts.setdefault(file_id, []).append(
            FakePart(file_id, idx, channel_id, message_id, doc_id, size, original_filename)
        )

    async def set_fields(self, node_id: str, **fields: Any) -> None:
        node = self.nodes[node_id]
        self.nodes[node_id] = replace(node, **fields)

    async def purge(self, node_id: str) -> None:
        self.purged.append(node_id)
        self.nodes.pop(node_id, None)
        self.parts.pop(node_id, None)

    async def create(self, **fields: Any) -> FakeNode:
        node_id = fields.pop("node_id", None) or f"created-{len(self.nodes)}"
        node = FakeNode(id=node_id, **fields)
        self.nodes[node.id] = node
        return node


class RecordingGateway:
    def __init__(self) -> None:
        self.caption_edits: list[tuple[int, int, str]] = []

    async def edit_message_caption(self, channel_id: int, message_id: int, caption: str) -> None:
        self.caption_edits.append((channel_id, message_id, caption))


class TelegramBackedUploader:
    async def upload(self, *args: Any, **kwargs: Any) -> UploadResult:
        records = (
            PartRecord(
                idx=0,
                channel_id=-100,
                message_id=11,
                doc_id=101,
                size=10,
                original_filename=f"{kwargs['filename']}.001",
            ),
            PartRecord(
                idx=1,
                channel_id=-100,
                message_id=12,
                doc_id=102,
                size=10,
                original_filename=f"{kwargs['filename']}.002",
            ),
        )
        for record in records:
            await kwargs["on_part"](record)
        return UploadResult(size=20, parts=records)


def make_fs(
    repo: FakeRepo,
    gateway: RecordingGateway,
    *,
    caption_template: str = DEFAULT_CAPTION_TEMPLATE,
) -> FileSystem:
    return FileSystem(
        repo,
        master_channel=-100,
        gateway=gateway,
        caption_template=caption_template,
    )


async def source_chunks():
    yield b"x" * 20


@pytest.mark.asyncio
async def test_write_resyncs_multipart_captions_after_telegram_upload():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["root"] = FakeNode(id="root", name="", parent_id=None, is_folder=True)

    node = await FileSystem(
        repo,
        master_channel=-100,
        gateway=gateway,
        uploader=TelegramBackedUploader(),
        min_size=1,
        caption_template="fileName: {filename}; part: {part_idx}/{parts}",
    ).write("root", "Movie.mkv", source_chunks)

    assert node.name == "Movie.mkv"
    assert gateway.caption_edits == [
        (-100, 11, "fileName: Movie.mkv.001; part: 1/2"),
        (-100, 12, "fileName: Movie.mkv.002; part: 2/2"),
    ]


@pytest.mark.asyncio
async def test_rename_syncs_telegram_captions_without_changing_original_filenames():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["file"] = FakeNode(id="file", name="Old.mkv", size=20)
    repo.parts["file"] = [
        FakePart("file", 0, -100, 11, 101, 10, "Physical Old.mkv.001"),
        FakePart("file", 1, -100, 12, 102, 10, "Physical Old.mkv.002"),
    ]

    node = await make_fs(repo, gateway).rename("file", "New.mkv")

    assert node.name == "New.mkv"
    assert gateway.caption_edits == [
        (-100, 11, "fileName: New.mkv.001"),
        (-100, 12, "fileName: New.mkv.002"),
    ]
    assert [part.original_filename for part in repo.parts["file"]] == [
        "Physical Old.mkv.001",
        "Physical Old.mkv.002",
    ]


@pytest.mark.asyncio
async def test_rename_skips_caption_sync_when_template_does_not_depend_on_changed_values():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["file"] = FakeNode(id="file", name="Old.mkv", size=20)
    repo.parts["file"] = [
        FakePart("file", 0, -100, 11, 101, 20, "Physical Old.mkv"),
    ]

    node = await make_fs(repo, gateway, caption_template="{path}").rename("file", "New.mkv")

    assert node.name == "New.mkv"
    assert gateway.caption_edits == []


@pytest.mark.asyncio
async def test_rename_uses_caption_template_with_parent_path_and_part_size():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["root"] = FakeNode(id="root", name="", parent_id=None, is_folder=True)
    repo.nodes["backup"] = FakeNode(id="backup", name="backup", parent_id="root", is_folder=True)
    repo.nodes["file"] = FakeNode(
        id="file", name="Old.mkv", parent_id="backup", size=30, mime="video/x-matroska"
    )
    repo.parts["file"] = [
        FakePart("file", 0, -100, 11, 101, 10, "Physical Old.mkv.001"),
        FakePart("file", 1, -100, 12, 102, 20, "Physical Old.mkv.002"),
    ]

    await make_fs(
        repo,
        gateway,
        caption_template="{path}\nfileName: {filename}\npart: {part_idx}/{parts}\nsize: {size}",
    ).rename("file", "New.mkv")

    assert gateway.caption_edits == [
        (-100, 11, "/backup\nfileName: New.mkv.001\npart: 1/2\nsize: 10"),
        (-100, 12, "/backup\nfileName: New.mkv.002\npart: 2/2\nsize: 20"),
    ]


@pytest.mark.asyncio
async def test_set_mime_syncs_caption_when_template_depends_on_mime():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["root"] = FakeNode(id="root", name="", parent_id=None, is_folder=True)
    repo.nodes["file"] = FakeNode(
        id="file", name="Movie.mkv", parent_id="root", size=20, mime="video/x-matroska"
    )
    repo.parts["file"] = [
        FakePart("file", 0, -100, 11, 101, 20, "Movie.mkv"),
    ]

    node = await make_fs(repo, gateway, caption_template="{mime}").set_mime(
        "file", "video/x-msvideo"
    )

    assert node.mime == "video/x-msvideo"
    assert gateway.caption_edits == [(-100, 11, "video/x-msvideo")]


@pytest.mark.asyncio
async def test_set_info_notes_syncs_caption_when_template_depends_on_info():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["root"] = FakeNode(id="root", name="", parent_id=None, is_folder=True)
    repo.nodes["file"] = FakeNode(
        id="file", name="Movie.mkv", parent_id="root", size=20, info={"rating": "ok"}
    )
    repo.parts["file"] = [FakePart("file", 0, -100, 11, 101, 20, "Movie.mkv")]

    node = await make_fs(
        repo, gateway, caption_template="fileName: {filename}\n{info}"
    ).set_info_notes("file", "Director cut")

    assert node.info == {"rating": "ok", "notes": "Director cut"}
    assert gateway.caption_edits == [(-100, 11, "fileName: Movie.mkv\nDirector cut")]


@pytest.mark.asyncio
async def test_set_info_notes_skips_caption_when_template_ignores_info():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["file"] = FakeNode(id="file", name="Movie.mkv", size=20)
    repo.parts["file"] = [FakePart("file", 0, -100, 11, 101, 20, "Movie.mkv")]

    node = await make_fs(
        repo, gateway, caption_template="fileName: {filename}"
    ).set_info_notes("file", "Director cut")

    assert node.info == {"notes": "Director cut"}
    assert gateway.caption_edits == []


@pytest.mark.asyncio
async def test_resync_caption_forces_caption_update():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["root"] = FakeNode(id="root", name="", parent_id=None, is_folder=True)
    repo.nodes["file"] = FakeNode(
        id="file", name="Movie.mkv", parent_id="root", size=20, info={"notes": "TMDB: 33333"}
    )
    repo.parts["file"] = [FakePart("file", 0, -100, 11, 101, 20, "Movie.mkv")]

    await make_fs(repo, gateway, caption_template="{info}").resync_caption("file")

    assert gateway.caption_edits == [(-100, 11, "TMDB: 33333")]


@pytest.mark.asyncio
async def test_merge_syncs_caption_to_final_logical_name_and_preserves_original_filenames():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["target"] = FakeNode(id="target", name="First.mkv", parent_id="root", size=10)
    repo.nodes["donor"] = FakeNode(id="donor", name="Second.mkv", parent_id="root", size=10)
    repo.parts["target"] = [FakePart("target", 0, -100, 11, 101, 10, "First physical.mkv")]
    repo.parts["donor"] = [FakePart("donor", 0, -100, 12, 102, 10, "Second physical.mkv")]

    node = await make_fs(repo, gateway).merge_parts("target", ["donor"], name="Merged.mkv")

    assert node.name == "Merged.mkv"
    assert gateway.caption_edits == [
        (-100, 11, "fileName: Merged.mkv.001"),
        (-100, 12, "fileName: Merged.mkv.002"),
    ]
    assert [part.original_filename for part in repo.parts["target"]] == [
        "First physical.mkv",
        "Second physical.mkv",
    ]


@pytest.mark.asyncio
async def test_reorder_syncs_caption_suffixes_to_new_order():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["file"] = FakeNode(id="file", name="Movie.mkv", size=30)
    repo.parts["file"] = [
        FakePart("file", 0, -100, 11, 101, 10, "Movie.mkv.001"),
        FakePart("file", 1, -100, 12, 102, 10, "Movie.mkv.002"),
        FakePart("file", 2, -100, 13, 103, 10, "Movie.mkv.003"),
    ]

    await make_fs(repo, gateway).reorder_parts("file", [2, 0, 1])

    assert gateway.caption_edits == [
        (-100, 13, "fileName: Movie.mkv.001"),
        (-100, 11, "fileName: Movie.mkv.002"),
        (-100, 12, "fileName: Movie.mkv.003"),
    ]
    assert [part.original_filename for part in repo.parts["file"]] == [
        "Movie.mkv.003",
        "Movie.mkv.001",
        "Movie.mkv.002",
    ]


@pytest.mark.asyncio
async def test_split_syncs_source_and_extracted_file_captions():
    repo = FakeRepo()
    gateway = RecordingGateway()
    repo.nodes["file"] = FakeNode(id="file", name="Movie.mkv", parent_id="root", size=30)
    repo.parts["file"] = [
        FakePart("file", 0, -100, 11, 101, 10, "Movie.mkv.001"),
        FakePart("file", 1, -100, 12, 102, 10, "Movie.mkv.002"),
        FakePart("file", 2, -100, 13, 103, 10, "Movie.mkv.003"),
    ]

    source, extracted = await make_fs(repo, gateway).split_parts("file", [1])

    assert source.name == "Movie.mkv"
    assert [node.name for node in extracted] == ["Movie.mkv.002"]
    assert gateway.caption_edits == [
        (-100, 11, "fileName: Movie.mkv.001"),
        (-100, 13, "fileName: Movie.mkv.002"),
        (-100, 12, "fileName: Movie.mkv.002"),
    ]
