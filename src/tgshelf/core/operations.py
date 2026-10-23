"""One shared file budget, fresh admission, and snapshot-bound account leases."""
import asyncio
from contextlib import asynccontextmanager

from tgshelf.core.access import NoAuthorizedAccount
from tgshelf.telegram.errors import FloodCooldown


class OperationScheduler:
    def __init__(self, concurrent: int):
        if concurrent < 1:
            raise ValueError("Operation concurrency must be positive")
        self.concurrent = concurrent
        self._semaphore = asyncio.Semaphore(concurrent)

    @asynccontextmanager
    async def slot(self):
        async with self._semaphore:
            yield


def recovery_wait(pool, names):
    """Include recoverable quarantine without widening the candidate mask."""
    now = pool.now()
    waits = [max(m.cooldown_until, m.quarantine_until) - now for m in pool.members
             if m.name in names and max(m.cooldown_until, m.quarantine_until) > now]
    return min(waits) if waits else None


class OperationContext:
    def __init__(self, admission, snapshot, member):
        self.admission = admission
        self.snapshot = snapshot
        self.member = member
        self._tried = set()

    @property
    def gateway(self):
        if self.member is None:
            raise RuntimeError("Operation has no Telegram account")
        return BoundAccountGateway(self.member, self.admission.pool,
                                   limiter=self.admission.limiter, sleep=self.admission.sleep)

    async def advance_after_denial(self):
        if self.member is not None:
            self._tried.add(self.member.name)
            self.admission.pool.release(self.member)
            self.member = None
        names = self.snapshot.user_accounts - self._tried
        while names:
            member = self.admission.pool.replace(allowed_names=names)
            if member is not None:
                self.admission.pool.acquire(member)
                self.member = member
                return True
            wait = recovery_wait(self.admission.pool, names)
            if wait is None:
                raise RuntimeError("Authorized alternative accounts are unavailable")
            await self.admission.sleep(max(wait, 0))
        return False


class AccessAdmission:
    def __init__(self, service, pool, scheduler, *, limiter=None, sleep=asyncio.sleep):
        self.service, self.pool, self.scheduler = service, pool, scheduler
        self.limiter, self.sleep = limiter, sleep

    @asynccontextmanager
    async def start(self, requirements, *, telegram_required=True, independent=False, extra_requirements=()):
        if independent and telegram_required:
            raise ValueError("Independent folder authorization cannot lease a Telegram account")
        async with self.scheduler.slot():
            while True:
                snapshot = await (self.service.snapshot_independent(requirements) if independent
                                  else self.service.snapshot(requirements))
                if extra_requirements:
                    await self.service.snapshot_independent(extra_requirements)
                if not snapshot.user_accounts:
                    raise NoAuthorizedAccount("No authorized user account for this operation")
                member = self.pool.replace(allowed_names=snapshot.user_accounts) if telegram_required else None
                if member is not None:
                    self.pool.acquire(member)
                if not telegram_required or member is not None:
                    break
                wait = recovery_wait(self.pool, snapshot.user_accounts)
                if wait is None:
                    raise RuntimeError("Authorized accounts are unavailable")
                await self.sleep(max(wait, 0))
            context = OperationContext(self, snapshot, member)
            try:
                yield context
            finally:
                if context.member is not None:
                    self.pool.release(context.member)


class BoundAccountGateway:
    """Keep one account for all writes; preserve existing per-account pacing."""
    def __init__(self, member, pool, *, limiter=None, sleep=asyncio.sleep):
        self.member, self.pool, self.limiter, self.sleep = member, pool, limiter, sleep

    def __getattr__(self, name):
        return getattr(self.member.client, name)

    async def _write(self, method, *args, **kwargs):
        while True:
            wait = max(self.member.cooldown_until, self.member.quarantine_until) - self.pool.now()
            if wait > 0:
                await self.sleep(wait)
            self.pool.refresh_quarantines()
            if self.limiter is not None:
                wait = self.limiter.acquire(self.member.name)
                if wait > 0:
                    await self.sleep(wait)
                    continue
                kwargs["rate_limit"] = False
            try:
                return await getattr(self.member.client, method)(*args, **kwargs)
            except FloodCooldown as exc:
                self.pool.mark_flood(self.member, exc.seconds)

    async def copy_message(self, *args, **kwargs):
        return await self._write("copy_message", *args, **kwargs)

    async def delete_message(self, *args, **kwargs):
        return await self._write("delete_message", *args, **kwargs)

    async def edit_message_caption(self, *args, **kwargs):
        return await self._write("edit_message_caption", *args, **kwargs)

    async def send_document(self, *args, **kwargs):
        return await self._write("send_document", *args, **kwargs)
