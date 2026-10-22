"""Incremental import of a mapped folder's Telegram channel history."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import sys

from tgshelf.config import Config
from tgshelf.core.access import AccessRequirement, ChannelAccessService
from tgshelf.core.fs import FileSystem
from tgshelf.core.import_channel import ChannelImporter, ImportHistoryReader, ImportOptions, ImportRunError, ImportStats
from tgshelf.core.import_filters import parse_filters
from tgshelf.db.engine import create_engine, create_session_factory
from tgshelf.db.repo import NodeRepo
from tgshelf.log import describe_exc

log = logging.getLogger('tgshelf.import')


def _has_file(message) -> bool:
    """Compatibility predicate shared with the dedicated live watcher."""
    return getattr(message, 'media', None) is not None


async def run(config: Config, args) -> int:
    try:
        filters = parse_filters(**{name: getattr(args, name, ()) or () for name in
            ('include_ext', 'exclude_ext', 'include_mime', 'exclude_mime')})
        limit = getattr(args, 'limit', 0)
        if limit < 0:
            raise ValueError('Import limit must be non-negative')
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    from tgshelf.http.serve import build_runtime, make_write_limiter, start_clients
    engine = create_engine(config.db)
    sessions = create_session_factory(engine)
    pairs = []
    stats = ImportStats()
    failed = False
    try:
        async with sessions() as session:
            fs = FileSystem(NodeRepo(session), master_channel=config.telegram.upload.channel)
            node = await fs.resolve(getattr(args, 'path', '/'))
            if node is None or not node.is_folder or node.state != 'ACTIVE':
                raise ValueError('Import destination must be an existing active folder')
            folder_id = node.id
            channel = await fs.effective_channel(folder_id)
        policy = ChannelAccessService(sessions, config.telegram.users, config.telegram.upload.channel)
        # Fail closed before startup when no configured user has logical read access.
        await policy.snapshot([AccessRequirement(channel, 'read')])
        limiter = make_write_limiter(config.operations)
        user_config = replace(config, telegram=replace(config.telegram,
            users=tuple(account for account in config.telegram.users if not account.is_bot)))
        pairs = await start_clients(user_config, limiter)
        runtime = build_runtime(config, sessions, pairs, write_limiter=limiter)
        admission, scheduler = runtime['admission'], runtime['operation_scheduler']
        def fs_factory(session):
            return FileSystem(NodeRepo(session), master_channel=config.telegram.upload.channel,
                caption_template=config.caption.template, min_size=config.telegram.upload.min_size,
                admission=admission, plugin_manager=runtime['plugin_manager'])
        importer = ChannelImporter(sessions, engine, ImportHistoryReader(admission),
                                   admission, scheduler, fs_factory)
        stats = await importer.run(ImportOptions(folder_id, channel, getattr(args, 'rescan', False), filters, limit))
        failed = stats.failed > 0
    except ImportRunError as exc:
        stats = exc.stats
        failed = True
        log.error('[import] reason=import_aborted error=%s', describe_exc(exc.cause))
    except asyncio.CancelledError:
        log.warning('[import] reason=cancelled; unfinished page will be replayed on the next run')
        raise
    except Exception as exc:
        failed = True
        log.error('[import] reason=import_aborted error=%s', describe_exc(exc))
    finally:
        for _account, client in pairs:
            try:
                await _disconnect(client)
            except Exception:
                failed = True
                log.exception('[import] reason=client_disconnect_failed')
        await engine.dispose()
    log.info('[import] done: %s', stats)
    print(f'import-channel: {stats}')
    return 1 if failed else 0


async def _disconnect(client) -> None:
    close = getattr(client, 'aclose', None)
    try:
        if close is not None:
            await close()
    finally:
        disconnect = getattr(getattr(client, '_client', None), 'disconnect', None)
        if disconnect is not None:
            await disconnect()
