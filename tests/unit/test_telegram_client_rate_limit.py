from __future__ import annotations

import pytest

from tgshelf.telegram.client import TgClient


class FakeTelethonClient:
    def __init__(self):
        self.requests = []
        self.session = type("Session", (), {"dc_id": 1})()

    async def __call__(self, request):
        self.requests.append(request)
        return sent_result()

    async def get_messages(self, channel_id, ids=None, limit=None):
        self.requests.append(("get_messages", channel_id, ids, limit))
        return fake_message()

    async def get_input_entity(self, entity):
        self.requests.append(("get_input_entity", entity))
        return entity

    async def delete_messages(self, entity, message_ids):
        self.requests.append(("delete_messages", entity, message_ids))
        return [type("Affected", (), {"pts_count": 1})()]


def fake_message():
    document = type(
        "Document",
        (),
        {
            "id": 42,
            "access_hash": 99,
            "file_reference": b"ref",
            "dc_id": 1,
            "size": 123,
            "mime_type": "video/mp4",
            "attributes": [],
        },
    )()
    media = type("Media", (), {"document": document})()
    return type(
        "Message",
        (),
        {"id": 7, "media": media, "document": document, "message": "caption", "empty": False},
    )()


def sent_result():
    message = fake_message()
    update = type("Update", (), {"message": message})()
    return type("Result", (), {"updates": [update]})()


class RecordingRateLimiter:
    def __init__(self):
        self.accounts = []

    def acquire(self, account: str) -> float:
        self.accounts.append(account)
        return 0.0


@pytest.mark.asyncio
async def test_invoke_does_not_rate_limit_by_default():
    raw = FakeTelethonClient()
    limiter = RecordingRateLimiter()
    client = TgClient(raw, name="main", rate_limiter=limiter)

    await client.invoke("control-call")

    assert limiter.accounts == []
    assert raw.requests == ["control-call"]


@pytest.mark.asyncio
async def test_get_document_does_not_rate_limit_read_calls():
    raw = FakeTelethonClient()
    limiter = RecordingRateLimiter()
    client = TgClient(raw, name="main", rate_limiter=limiter)

    doc = await client.get_document(channel_id=-100, message_id=7)

    assert doc is not None
    assert doc.caption == "caption"
    assert limiter.accounts == []


@pytest.mark.asyncio
async def test_save_big_part_does_not_rate_limit_upload_chunks():
    raw = FakeTelethonClient()
    limiter = RecordingRateLimiter()
    client = TgClient(raw, name="main", rate_limiter=limiter)

    await client.save_big_part(file_id=1, part_idx=0, total_parts=1, data=b"chunk")

    assert limiter.accounts == []
    assert raw.requests[0].__class__.__name__ == "SaveBigFilePartRequest"


@pytest.mark.asyncio
async def test_send_document_rate_limits_the_publish_write():
    raw = FakeTelethonClient()
    limiter = RecordingRateLimiter()
    client = TgClient(raw, name="main", rate_limiter=limiter)

    await client.send_document(
        channel_id=-100,
        file_id=1,
        total_parts=1,
        filename="movie.mkv",
        size=123,
        mime="video/x-matroska",
        caption="caption",
    )

    assert limiter.accounts == ["main"]


@pytest.mark.asyncio
async def test_copy_message_rate_limits_only_the_send_write():
    raw = FakeTelethonClient()
    limiter = RecordingRateLimiter()
    client = TgClient(raw, name="main", rate_limiter=limiter)

    await client.copy_message(from_channel_id=-100, message_id=7, to_channel_id=-200)

    assert limiter.accounts == ["main"]
    assert any(req[0] == "get_messages" for req in raw.requests if isinstance(req, tuple))


@pytest.mark.asyncio
async def test_delete_message_rate_limits_the_delete_write():
    raw = FakeTelethonClient()
    limiter = RecordingRateLimiter()
    client = TgClient(raw, name="main", rate_limiter=limiter)

    deleted = await client.delete_message(channel_id=-100, message_id=7)

    assert deleted is True
    assert limiter.accounts == ["main"]
