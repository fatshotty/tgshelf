from __future__ import annotations

import pytest

from tgshelf.core.channels import forward_parts
from tgshelf.core.upload import PartRecord
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
        caption_factory=lambda p: f"filename: restored-{p.idx}.mkv",
    )

    assert gateway.copies == [(-100, 7, -200, "filename: restored-0.mkv")]
