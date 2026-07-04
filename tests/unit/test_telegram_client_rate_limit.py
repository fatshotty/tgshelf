from __future__ import annotations

import pytest

from tgshelf.telegram.client import TgClient
from tgshelf.telegram.pool import ClientPool, PoolMember
from tgshelf.telegram.ratelimit import TokenBucketRateLimiter
from tgshelf.telegram.write_gateway import AccountWriteGateway


class Clock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


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

    async def edit_message(self, entity, message_id, text):
        self.requests.append(("edit_message", entity, message_id, text))
        return fake_message()


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


class RecordingWriteClient:
    def __init__(self, name: str):
        self.name = name
        self.copied = []

    async def copy_message(
        self,
        from_channel_id: int,
        message_id: int,
        to_channel_id: int,
        *,
        caption: str | None = None,
        rate_limit: bool = True,
    ):
        self.copied.append(
            (from_channel_id, message_id, to_channel_id, caption, rate_limit)
        )
        return (message_id + 1000, 42)


def test_token_bucket_allows_burst_then_refills_gradually():
    clock = Clock()
    limiter = TokenBucketRateLimiter(capacity=2, refill_seconds=10, clock=clock)

    assert limiter.acquire("main") == 0.0
    assert limiter.acquire("main") == 0.0
    assert limiter.acquire("main") == 5.0

    clock.now = 4.0
    assert limiter.acquire("main") == pytest.approx(1.0)

    clock.now = 5.0
    assert limiter.acquire("main") == 0.0


@pytest.mark.asyncio
async def test_account_write_gateway_skips_full_account_and_uses_available_account():
    clock = Clock()
    limiter = TokenBucketRateLimiter(capacity=1, refill_seconds=10, clock=clock)
    assert limiter.acquire("a") == 0.0
    client_a = RecordingWriteClient("a")
    client_b = RecordingWriteClient("b")
    pool = ClientPool(
        [
            PoolMember(client_a, name="a"),
            PoolMember(client_b, name="b"),
        ]
    )
    gateway = AccountWriteGateway(pool, limiter=limiter)

    await gateway.copy_message(-100, 10, -200)

    assert client_a.copied == []
    assert client_b.copied == [(-100, 10, -200, None, False)]


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
async def test_copy_message_can_override_caption():
    raw = FakeTelethonClient()
    client = TgClient(raw, name="main")

    await client.copy_message(
        from_channel_id=-100,
        message_id=7,
        to_channel_id=-200,
        caption="fileName: canonical.mkv",
    )

    send_request = next(
        req for req in raw.requests if req.__class__.__name__ == "SendMediaRequest"
    )
    assert send_request.message == "fileName: canonical.mkv"


@pytest.mark.asyncio
async def test_delete_message_rate_limits_the_delete_write():
    raw = FakeTelethonClient()
    limiter = RecordingRateLimiter()
    client = TgClient(raw, name="main", rate_limiter=limiter)

    deleted = await client.delete_message(channel_id=-100, message_id=7)

    assert deleted is True
    assert limiter.accounts == ["main"]


@pytest.mark.asyncio
async def test_edit_message_caption_rate_limits_the_write():
    raw = FakeTelethonClient()
    limiter = RecordingRateLimiter()
    client = TgClient(raw, name="main", rate_limiter=limiter)

    await client.edit_message_caption(-100, 7, "fileName: canonical.mkv")

    assert limiter.accounts == ["main"]
    assert ("edit_message", -100, 7, "fileName: canonical.mkv") in raw.requests
