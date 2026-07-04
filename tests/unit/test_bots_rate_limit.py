from __future__ import annotations

import pytest

from tgshelf.commands import bots


class RecordingRateLimiter:
    def __init__(self, waits=None):
        self.waits = list(waits or [])
        self.accounts = []

    def acquire(self, account: str) -> float:
        self.accounts.append(account)
        return self.waits.pop(0) if self.waits else 0.0


class FakeBotFatherClient:
    def __init__(self):
        self.sent = []
        self.get_messages_calls = 0

    async def get_messages(self, peer, limit):
        self.get_messages_calls += 1
        if self.get_messages_calls == 1:
            return [type("Message", (), {"id": 1, "out": False, "message": "old"})()]
        return [type("Message", (), {"id": 2, "out": False, "message": "reply"})()]

    async def send_message(self, peer, text):
        self.sent.append((peer, text))


class FakeAdminClient:
    def __init__(self):
        self.requests = []

    async def get_input_entity(self, entity):
        return entity

    async def __call__(self, request):
        self.requests.append(request)


@pytest.mark.asyncio
async def test_send_and_wait_rate_limits_botfather_writes():
    client = FakeBotFatherClient()
    limiter = RecordingRateLimiter(waits=[2.0, 0.0])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    reply = await bots.send_and_wait(
        client,
        "/newbot",
        poll=0.1,
        sleep=fake_sleep,
        rate_limiter=limiter,
        account_name="main",
    )

    assert reply == "reply"
    assert limiter.accounts == ["main", "main"]
    assert sleeps == [2.0, 0.1]
    assert client.sent == [(bots.BOTFATHER, "/newbot")]


@pytest.mark.asyncio
async def test_promote_bot_rate_limits_each_admin_write(monkeypatch):
    client = FakeAdminClient()
    limiter = RecordingRateLimiter()
    sleeps = []
    monkeypatch.setattr(bots.utils, "get_input_channel", lambda value: value)
    monkeypatch.setattr(bots.utils, "get_input_user", lambda value: value)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    await bots.promote_bot(
        client,
        channel_id=-100,
        bot="@bot",
        sleep=fake_sleep,
        rate_limiter=limiter,
        account_name="main",
    )

    assert limiter.accounts == ["main", "main"]
    assert sleeps == [2]
    assert len(client.requests) == 2
