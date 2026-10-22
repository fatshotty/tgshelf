"""Monotonic import checkpoints and PostgreSQL advisory lock ownership."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from tgshelf.db.models import ChannelImportProgress


class ImportAlreadyRunning(RuntimeError):
    pass


class ImportProgressRepo:
    def __init__(self, session):
        self.session = session

    async def get_cursor(self, folder_id: str, channel_id: int) -> int:
        value = await self.session.scalar(select(ChannelImportProgress.last_scanned_message_id).where(
            ChannelImportProgress.folder_id == folder_id, ChannelImportProgress.channel_id == channel_id))
        return value if value is not None else 0

    async def advance(self, folder_id: str, channel_id: int, boundary: int) -> int:
        if boundary < 0:
            raise ValueError('Import checkpoint must be non-negative')
        stmt = insert(ChannelImportProgress).values(folder_id=folder_id, channel_id=channel_id,
                                                     last_scanned_message_id=boundary)
        stmt = stmt.on_conflict_do_update(
            index_elements=['folder_id', 'channel_id'],
            set_={'last_scanned_message_id': func.greatest(
                ChannelImportProgress.last_scanned_message_id, stmt.excluded.last_scanned_message_id),
                'updated_at': func.now()},
        ).returning(ChannelImportProgress.last_scanned_message_id)
        return await self.session.scalar(stmt)


def _lock_key(namespace: str, *values) -> int:
    data = ':'.join((namespace, *map(str, values))).encode()
    return int.from_bytes(hashlib.sha256(data).digest()[:8], 'big', signed=True)


async def import_message_lock(session, channel_id: int, message_id: int) -> None:
    await session.execute(text('SELECT pg_advisory_xact_lock(:key)'),
                          {'key': _lock_key('tgshelf-import-message', channel_id, message_id)})


class ImportRunLease:
    def __init__(self, connection, key: int, backend_pid: int):
        self.connection, self.key, self.backend_pid = connection, key, backend_pid
        self.lost = asyncio.Event()
        self._mutex = asyncio.Lock()

    async def check(self) -> None:
        async with self._mutex:
            try:
                if self.lost.is_set() or self.connection.closed or self.connection.invalidated:
                    raise ConnectionError('Import run lock connection was lost')
                pid = await self.connection.scalar(text('SELECT pg_backend_pid()'))
                await self.connection.commit()
                if pid != self.backend_pid:
                    raise ConnectionError('Import run lock session changed')
            except Exception as exc:
                self.lost.set()
                raise ConnectionError('Import run lock connection was lost; stopping import') from exc

    async def monitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(1)
                await self.check()
        except Exception:
            self.lost.set()


@asynccontextmanager
async def import_run_lock(engine, folder_id: str, channel_id: int):
    key = _lock_key('tgshelf-import-run', folder_id, channel_id)
    connection = await engine.connect()
    acquired = False
    monitor = None
    lease = None
    try:
        try:
            acquired = bool(await connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}))
            pid = await connection.scalar(text('SELECT pg_backend_pid()'))
            await connection.commit()
        except BaseException:
            await connection.invalidate()
            raise
        if not acquired:
            raise ImportAlreadyRunning(f'Import already running for folder {folder_id}, channel {channel_id}')
        lease = ImportRunLease(connection, key, pid)
        monitor = asyncio.create_task(lease.monitor())
        yield lease
    finally:
        async def cleanup():
            if monitor is not None:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            try:
                if acquired:
                    if connection.invalidated or (lease is not None and lease.lost.is_set()):
                        await connection.invalidate()
                    else:
                        if connection.in_transaction():
                            await connection.rollback()
                        unlocked = await connection.scalar(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
                        await connection.commit()
                        if not unlocked:
                            await connection.invalidate()
            except BaseException:
                await connection.invalidate()
                raise
            finally:
                await connection.close()
        task = asyncio.create_task(asyncio.wait_for(cleanup(), timeout=5))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
