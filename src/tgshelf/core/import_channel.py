"""Bounded, idempotent channel-history cataloging."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tgshelf.core.import_filters import ImportFilters
    from tgshelf.db.models import Node


@dataclass(frozen=True)
class ImportResult:
    node: Node | None
    outcome: Literal['imported', 'already_cataloged', 'unsupported']


class ImportHistoryReader:
    """One fresh read admission per page, with bounded candidates on failover."""

    def __init__(self, admission):
        self.admission = admission

    async def latest(self, channel_id: int) -> int:
        return await self._call(channel_id, 'latest_history_id')

    async def page(self, channel_id: int, *, after_id: int, through_id: int, limit: int):
        return await self._call(channel_id, 'history_page', after_id=after_id,
                                through_id=through_id, limit=limit)

    async def _call(self, channel_id, method, **kwargs):
        from tgshelf.core.access import AccessRequirement
        from tgshelf.core.operations import recovery_wait
        from tgshelf.telegram.errors import ChannelUnavailable, FloodCooldown, SessionDead
        import logging
        log = logging.getLogger('tgshelf.import')
        async with self.admission.start([AccessRequirement(channel_id, 'read')]) as ctx:
            denied = set()
            while True:
                try:
                    return await getattr(ctx.member.client, method)(channel_id, **kwargs)
                except (ChannelUnavailable, SessionDead):
                    denied.add(ctx.member.name)
                    log.warning('[import] channel=%s account=%s reason=reader_access_denied; trying an authorized alternative',
                                channel_id, ctx.member.name)
                    if not await ctx.advance_after_denial():
                        raise
                except FloodCooldown as exc:
                    pool = self.admission.pool
                    pool.mark_flood(ctx.member, exc.seconds)
                    pool.release(ctx.member)
                    ctx.member = None
                    names = ctx.snapshot.user_accounts - denied
                    while ctx.member is None:
                        member = pool.replace(allowed_names=names)
                        if member is not None:
                            pool.acquire(member)
                            ctx.member = member
                            break
                        wait = recovery_wait(pool, names)
                        if wait is None:
                            raise RuntimeError('Authorized import readers are unavailable') from exc
                        await self.admission.sleep(max(wait, 0))


@dataclass(frozen=True)
class ImportOptions:
    folder_id: str
    channel_id: int
    rescan: bool
    filters: 'ImportFilters'
    limit: int = 0


@dataclass
class ImportStats:
    examined: int = 0
    imported: int = 0
    already_cataloged: int = 0
    filtered: int = 0
    unsupported: int = 0
    failed: int = 0
    completed: bool = False
    stopped_by_limit: bool = False
    checkpoint: int = 0

    def __str__(self):
        from dataclasses import fields
        return ', '.join(f'{f.name}={getattr(self, f.name)}' for f in fields(self))


class ImportDestinationChanged(ValueError):
    pass


class ImportRunError(RuntimeError):
    def __init__(self, stats: ImportStats, cause: Exception):
        self.stats, self.cause = stats, cause
        super().__init__(str(cause))


class ChannelImporter:
    def __init__(self, session_factory, engine, reader, admission, scheduler, fs_factory, *, page_size=100):
        if not 1 <= page_size <= 100:
            raise ValueError('Import page size must be between 1 and 100')
        if admission.scheduler is not scheduler:
            raise ValueError('Import admission and workers must share one scheduler')
        self.sessions, self.engine, self.reader = session_factory, engine, reader
        self.admission, self.scheduler, self.fs_factory = admission, scheduler, fs_factory
        self.page_size = page_size

    async def _validate(self, session, options, *, lock=False):
        from tgshelf.db.repo import NodeRepo
        repo = NodeRepo(session)
        if lock:
            await repo.lock_import_destination(options.folder_id)
        fs = self.fs_factory(session)
        node = await repo.get(options.folder_id)
        if node is None or not node.is_folder or node.state != 'ACTIVE':
            raise ImportDestinationChanged('Import destination is not an active folder')
        if await fs.effective_channel(options.folder_id) != options.channel_id:
            raise ImportDestinationChanged('Import destination channel changed; start a new import')

    async def _guard(self, awaitable, lease):
        import asyncio
        task = asyncio.create_task(awaitable)
        lost = asyncio.create_task(lease.lost.wait())
        try:
            await asyncio.wait((task, lost), return_when=asyncio.FIRST_COMPLETED)
            if lease.lost.is_set():
                raise ConnectionError('Import run lock was lost; stopping import')
            return await task
        finally:
            for pending in (task, lost):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, lost, return_exceptions=True)

    async def run(self, options: ImportOptions) -> ImportStats:
        import logging
        from tgshelf.db.import_progress import ImportProgressRepo, import_run_lock
        stats = ImportStats()
        log = logging.getLogger('tgshelf.import')
        if options.limit < 0:
            raise ValueError('Import limit must be non-negative')
        try:
            async with import_run_lock(self.engine, options.folder_id, options.channel_id) as lease:
                async with self.sessions() as session:
                    await self._validate(session, options)
                    stats.checkpoint = await ImportProgressRepo(session).get_cursor(options.folder_id, options.channel_id)
                after = 0 if options.rescan else stats.checkpoint
                upper = await self._guard(self.reader.latest(options.channel_id), lease)
                log.info('[import] folder=%s channel=%s mode=%s after=%s through=%s concurrent=%s',
                         options.folder_id, options.channel_id, 'rescan' if options.rescan else 'incremental',
                         after, upper, self.scheduler.concurrent)
                if after >= upper:
                    await self._checkpoint(options, stats, max(after, upper), lease)
                    stats.completed = True
                    return stats
                while after < upper:
                    await lease.check()
                    async with self.sessions() as session:
                        await self._validate(session, options)
                    remaining = options.limit - stats.examined if options.limit else self.page_size
                    if remaining <= 0:
                        stats.stopped_by_limit = True
                        break
                    page = await self._guard(self.reader.page(options.channel_id, after_id=after,
                        through_id=upper, limit=min(self.page_size, remaining)), lease)
                    ids = [e.message_id for e in page.entries]
                    if ids != sorted(set(ids)) or any(not after < i <= upper for i in ids):
                        raise ValueError('History page is not an ascending bounded interval')
                    if not after < page.scanned_through <= upper or (ids and ids[-1] > page.scanned_through):
                        raise ValueError('History page did not provide a valid completed boundary')
                    if len(ids) > min(self.page_size, remaining):
                        raise ValueError('History page exceeds its requested limit')
                    async with self.sessions() as session:
                        from tgshelf.db.repo import NodeRepo
                        owners = await NodeRepo(session).files_by_messages(options.channel_id, ids)
                        owned = {message_id: node.id for message_id, node in owners.items()}
                    await self._guard(self._process_page(options, page, owned, stats, lease), lease)
                    await self._checkpoint(options, stats, page.scanned_through, lease)
                    after = page.scanned_through
                    if page.exhausted:
                        stats.completed = True
                        break
                    if options.limit and stats.examined >= options.limit:
                        stats.stopped_by_limit = True
                        break
                return stats
        except Exception as exc:
            raise ImportRunError(stats, exc) from exc

    async def _checkpoint(self, options, stats, boundary, lease):
        from tgshelf.db.import_progress import ImportProgressRepo
        await lease.check()
        async with self.sessions() as session:
            await self._validate(session, options, lock=True)
            checkpoint = await ImportProgressRepo(session).advance(options.folder_id, options.channel_id, boundary)
            await lease.check()
            await session.commit()
            stats.checkpoint = checkpoint

    async def _process_page(self, options, page, owned, stats, lease):
        import asyncio
        async with asyncio.TaskGroup() as tasks:
            for item in page.entries:
                tasks.create_task(self._process_file(options, item, owned.get(item.message_id), stats, lease))

    async def _process_file(self, options, item, owner, stats, lease):
        import logging
        from pathlib import PurePath
        from tgshelf.core.access import AccessRequirement
        from tgshelf.core.import_filters import evaluate_filters
        from tgshelf.db.repo import DuplicateNameError
        from tgshelf.telegram.errors import ChannelUnavailable, TgError
        log = logging.getLogger('tgshelf.import')
        filename = item.filename or f'message_{item.message_id}'
        base = (filename, options.channel_id, item.message_id)
        stats.examined += 1
        if owner is not None:
            stats.already_cataloged += 1
            log.info('[import] file=%r channel=%s message=%s reason=already_cataloged node=%s', *base, owner)
            return
        if item.media_kind == 'none':
            return
        if item.media_kind != 'document':
            stats.unsupported += 1
            log.warning('[import] file=%r channel=%s message=%s reason=unsupported_media', *base)
            return
        decision = evaluate_filters(options.filters, filename=item.filename, mime=item.mime)
        if not decision.accepted:
            stats.filtered += 1
            log.warning('[import] file=%r channel=%s message=%s reason=%s mime=%r ext=%r '
                'include_mime=%s include_ext=%s exclude_mime=%s exclude_ext=%s', *base,
                decision.reason, item.mime, PurePath(item.filename or '').suffix,
                sorted(options.filters.include_mime), sorted(options.filters.include_ext),
                sorted(options.filters.exclude_mime), sorted(options.filters.exclude_ext))
            return
        async with self.admission.start([AccessRequirement(options.channel_id, 'read')], telegram_required=False) as ctx:
            await lease.check()
            try:
                async with self.sessions() as session:
                    await self._validate(session, options)
                    fs = self.fs_factory(session)
                    result = await fs.catalog_channel_file(options.channel_id, item,
                        parent_id=options.folder_id, operation_context=ctx, run_lease=lease)
            except ImportDestinationChanged:
                raise
            except (ValueError, TgError, DuplicateNameError) as exc:
                stats.failed += 1
                reason = 'read_access_denied' if isinstance(exc, ChannelUnavailable) else 'file_import_failed'
                log.error('[import] file=%r channel=%s message=%s reason=%s error=%s', *base, reason, exc)
                return
            if result.outcome == 'imported':
                stats.imported += 1
            elif result.outcome == 'already_cataloged':
                stats.already_cataloged += 1
            else:
                stats.unsupported += 1
            log.info('[import] file=%r channel=%s message=%s reason=%s node=%s',
                     *base, result.outcome, result.node.id if result.node else None)
