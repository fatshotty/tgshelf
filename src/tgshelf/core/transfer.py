"""Whole-file access preflight and crash-aware transfer publication."""
import asyncio
import logging
from copy import copy
from dataclasses import dataclass, field

from sqlalchemy import select

from tgshelf.core import channels
from tgshelf.core.access import AccessRequirement
from tgshelf.db.models import Node, Part
from tgshelf.db.repo import NodeRepo

from tgshelf.core.upload import PartRecord
from tgshelf.telegram.errors import ChannelUnavailable, DocIdMismatch, PartMissing

log = logging.getLogger("tgshelf.transfer")


class PartAccessDenied(Exception):
    code = "part_access_denied"

    def __init__(self, attempts):
        self.attempts = tuple(attempts)
        self.channel_id = attempts[-1][1] if attempts else None
        super().__init__(f"No authorized account can read every part: {self.attempts}")


@dataclass(frozen=True)
class PreparedTransfer:
    account_name: str
    parts: tuple[PartRecord, ...]


async def prepare_transfer(context, parts, source_folder_channel: int) -> PreparedTransfer:
    attempts = []
    while context.member is not None:
        try:
            for part in parts:
                ref = await context.gateway.get_document(part.channel_id, part.message_id)
                if ref is None:
                    raise PartMissing(str(part.message_id), part.idx)
                if part.doc_id is not None and ref.doc_id != part.doc_id:
                    raise DocIdMismatch(part.doc_id, ref.doc_id)
            return PreparedTransfer(context.member.name, tuple(part_record(p) for p in parts))
        except ChannelUnavailable:
            attempts.append((context.member.name, part.channel_id, part.idx))
            if not await context.advance_after_denial():
                raise PartAccessDenied(attempts) from None
    raise PartAccessDenied(attempts)


class StaleTransfer(RuntimeError):
    code = "stale_transfer"


class FolderMoveSkipped(PartAccessDenied):
    def __init__(self, path, cause):
        super().__init__(cause.attempts)
        self.path = path
        self.args = (f"Folder move skipped because {path}: {cause}",)


@dataclass(frozen=True)
class TransferTarget:
    parent_id: str
    channel_id: int
    parent_path: str


def part_record(p):
    return PartRecord(p.idx, p.channel_id, p.message_id, p.doc_id, p.size, p.original_filename)


def node_version(n):
    return (n.id, n.parent_id, n.name, n.state, n.channel_id, n.size, n.mtime,
            repr(n.info), n.mime)


@dataclass(frozen=True)
class TransferVersion:
    node: tuple
    parts: tuple[PartRecord, ...]
    content: bytes | None
    ancestors: tuple


@dataclass
class StagedTransfer:
    source_id: str
    version: TransferVersion
    staging_id: str
    target: TransferTarget
    target_version: tuple
    account_name: str | None
    snapshot: object
    admission: object
    move: bool = False
    new_parts: list[PartRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class TransferService:
    async def _replacement(self, fs, replace_id):
        if replace_id is None:
            return ()
        tree = [await fs.get(replace_id), *await fs.repo.subtree(replace_id, state=None)]
        if tree[0] is None or tree[0].state != "ACTIVE":
            raise StaleTransfer("Replacement target is no longer active")
        versions = tuple(node_version(n) for n in tree)
        return versions

    async def replacement_requirements(self, fs, versions):
        requirements = []
        for version in versions:
            node = await fs.get(version[0])
            if node is None:
                raise StaleTransfer("Replacement disappeared before admission")
            requirements.append(AccessRequirement(await fs.effective_channel(
                node.id if node.is_folder else node.parent_id), "write"))
        return requirements

    async def assert_versions(self, fs, versions):
        for version in versions:
            node = await fs.get(version[0])
            if node is None or node_version(node) != version:
                raise StaleTransfer("Folder or node changed while waiting for admission")

    async def _replace(self, fs, versions, *, publish=True):
        if not versions:
            return
        await fs.repo.session.execute(select(Node).where(Node.id.in_([v[0] for v in versions]))
                                      .order_by(Node.id).with_for_update())
        tree = [await fs.get(versions[0][0]), *await fs.repo.subtree(versions[0][0], state=None)]
        if any(n is None for n in tree) or tuple(node_version(n) for n in tree) != versions:
            raise StaleTransfer("Replacement target changed during transfer")
        if publish:
            await fs.repo.set_state_subtree(versions[0][0], "DELETED", from_states=("ACTIVE", "TEMP"))

    async def named(self, fs, node_id, parent_id, name, *, copy_file, replace_id=None):
        source = await fs.ensure_move_allowed(node_id, parent_id)
        if replace_id == node_id:
            raise StaleTransfer("Cannot replace the transfer source")
        replacement = await self._replacement(fs, replace_id)
        extra = await self.replacement_requirements(fs, replacement)
        if not copy_file:
            return await self.move(fs, node_id, parent_id, name=name, replacement=replacement)
        target = TransferTarget(parent_id, await fs.effective_channel(parent_id), await fs._path_of_folder(parent_id))
        source_channel = await fs.effective_channel(source.id if source.is_folder else source.parent_id)
        reqs = [AccessRequirement(source_channel, "read"), AccessRequirement(target.channel_id, "write")]
        if source.is_folder:
            original_tree = tuple(node_version(n) for n in [source, *await fs.repo.subtree(node_id, state="ACTIVE")])
            destination_version = await self._tree_version(fs, parent_id)
            await fs.repo.session.commit()
            async with fs._admission.start(reqs, telegram_required=False, extra_requirements=extra):
                await self.assert_versions(fs, (*original_tree, *destination_version, *replacement))
                staged_root = await fs.repo.create(name=name, parent_id=parent_id, is_folder=True,
                                                   state="TEMP", info={"_transfer": {"source_id": node_id}})
                await fs.repo.session.commit()
            try:
                await fs._copy_folder(source, parent_id, force_copy=True, staging_root=staged_root)
                ids = [v[0] for v in (*original_tree, *destination_version)]
                await fs.repo.session.execute(select(Node).where(Node.id.in_(ids)).order_by(Node.id).with_for_update())
                current = [await fs.get(node_id), *await fs.repo.subtree(node_id, state="ACTIVE")]
                if tuple(node_version(n) for n in current) != original_tree or await self._tree_version(fs, parent_id) != destination_version:
                    raise StaleTransfer("Folder transfer source or destination changed")
                await self._replace(fs, replacement)
                await fs.repo.set_state_subtree(staged_root.id, "ACTIVE", from_states=("TEMP",))
                await fs.repo.set_fields(staged_root.id, info={})
                await fs.repo.session.commit()
                return await fs.get(staged_root.id)
            except BaseException:
                await fs.repo.session.rollback()
                # Preserve the TEMP tree and all message references for recovery.
                log.error("[transfer] unpublished folder staging retained node=%s", staged_root.id)
                raise
        expected = await self.version(fs, source)
        destination_version = await self._tree_version(fs, parent_id)
        parts = expected.parts
        await fs.repo.session.commit()
        async with fs._admission.start(reqs, telegram_required=bool(parts), extra_requirements=extra) as ctx:
            await self.assert_versions(fs, (*destination_version, *replacement))
            await fs.repo.session.commit()
            prepared = await prepare_transfer(ctx, parts, source_channel) if parts else PreparedTransfer("", ())
            await self.assert_versions(fs, destination_version)
            worker = copy(fs)
            worker._operation_context, worker._executor = ctx, None
            worker._gateway = ctx.gateway if ctx.member else fs._gateway
            worker._admitted_versions = destination_version
            old_path = await worker.path_of(source.id)
            new_path = await worker._logical_child_path(parent_id, name)
            source_plugin_node = await worker._plugin_node_snapshot(source.id) if worker._plugins_enabled() else None
            await worker._run_before_plugin("before_file_copy", operation="file_copy", node=source,
                old_parent_id=source.parent_id, new_parent_id=parent_id, old_path=old_path,
                new_path=new_path, source_node=source_plugin_node)
            staged = await self.stage(worker, source, target, ctx, prepared, name=name, expected=expected)
            try:
                await self.validate(worker, staged)
                await self._replace(worker, replacement)
                await fs.repo.set_fields(staged.staging_id, state="ACTIVE", info=dict(source.info or {}))
                await fs.repo.session.commit()
                fs.last_account_name = staged.account_name
                result = await fs.get(staged.staging_id)
                await worker._run_after_plugin("after_file_copy", operation="file_copy", node=result,
                    old_parent_id=source.parent_id, new_parent_id=parent_id, old_path=old_path,
                    new_path=new_path, source_node=source_plugin_node,
                    target_node=await worker._plugin_node_snapshot(result.id) if worker._plugins_enabled() else None)
                return result
            except BaseException:
                await fs.repo.session.rollback()
                await self.discard(worker, staged, context=ctx)
                raise

    async def move(self, fs, node_id, new_parent_id, *, name=None, replacement=()):
        node = await fs.ensure_move_allowed(node_id, new_parent_id)
        name = name or node.name
        if node.parent_id == new_parent_id and name == node.name and not replacement:
            return node
        if not node.is_folder:
            target = TransferTarget(new_parent_id, await fs.effective_channel(new_parent_id),
                                    await fs._path_of_folder(new_parent_id))
            source_channel = await fs.effective_channel(node.parent_id)
            parts = tuple(part_record(p) for p in await fs.repo.parts_of(node_id))
            expected = await self.version(fs, node)
            destination_version = await self._tree_version(fs, new_parent_id)
            extra = await self.replacement_requirements(fs, replacement)
            requirements = [AccessRequirement(source_channel, "write"), AccessRequirement(target.channel_id, "write")]
            await fs.repo.session.commit()
            async with fs._admission.start(requirements, telegram_required=bool(parts), extra_requirements=extra) as ctx:
                await self.assert_versions(fs, (*destination_version, *replacement))
                await fs.repo.session.commit()
                prepared = await prepare_transfer(ctx, parts, source_channel) if parts else PreparedTransfer("", ())
                await self.assert_versions(fs, destination_version)
                worker = copy(fs)
                worker._operation_context, worker._executor = ctx, None
                worker._gateway = ctx.gateway if ctx.member else fs._gateway
                worker._admitted_versions = destination_version
                old_parent, old_path = node.parent_id, await fs.path_of(node_id)
                new_path = await fs._logical_child_path(new_parent_id, name)
                await worker._run_before_plugin("before_file_move", operation="file_move", node=node,
                    old_parent_id=old_parent, new_parent_id=new_parent_id, old_path=old_path, new_path=new_path)
                staged = await self.stage(worker, node, target, ctx, prepared, move=True, name=name, expected=expected)
                try:
                    await self.validate(worker, staged)
                    await self._replace(worker, replacement)
                    await self._publish_move(worker, staged, new_parent_id)
                    await fs.repo.session.commit()
                    result = await fs.get(node_id)
                    await fs.repo.session.commit()
                except BaseException:
                    await fs.repo.session.rollback()
                    await self.discard(worker, staged, context=ctx)
                    raise
                await self.cleanup_originals(worker, staged, ctx)
                try:
                    await worker._sync_part_captions(node_id, changed_fields={"path", "filename"})
                except Exception as exc:
                    staged.warnings.append(f"Caption synchronization failed: {exc}")
                await worker._run_after_plugin("after_file_move", operation="file_move", node=result,
                    old_parent_id=old_parent, new_parent_id=new_parent_id, old_path=old_path, new_path=new_path)
                fs.last_account_name, fs.last_warnings = staged.account_name, staged.warnings
                return result
        return await self._move_folder(fs, node, new_parent_id, name=name, replacement=replacement)

    async def _move_folder(self, fs, root, new_parent_id, *, name=None, replacement=()):
        root_id = root.id
        tree = [root, *await fs.repo.subtree(root_id, state="ACTIVE")]
        original_tree = tuple(node_version(n) for n in tree)
        destination_version = await self._tree_version(fs, new_parent_id)
        old_path = await fs.path_of(root_id)
        new_path = await fs._logical_child_path(new_parent_id, name or root.name)
        destination_channel = await fs.effective_channel(new_parent_id)
        requirements = [AccessRequirement(await fs.effective_channel(root_id), "write"),
                        AccessRequirement(destination_channel, "write")]
        extra = await self.replacement_requirements(fs, replacement)
        await fs.repo.session.commit()
        async with fs._admission.start(requirements, telegram_required=False, extra_requirements=extra):
            await self.assert_versions(fs, (*original_tree, *destination_version, *replacement))
        staged_files = []
        hook_contexts = []
        try:
            plans = []
            for file in tree:
                if file.is_folder:
                    folder_channel = await fs.effective_channel(file.id)
                    await fs.repo.session.commit()
                    async with fs._admission.start([AccessRequirement(folder_channel, "write")], telegram_required=False):
                        pass
                    continue
                ancestors = await fs.repo.ancestors(file.id)
                inside = False
                target_channel = destination_channel
                for ancestor in ancestors:
                    if ancestor.id == root_id:
                        inside = True
                    if inside and ancestor.channel_id is not None:
                        target_channel = ancestor.channel_id
                current_path = await fs.path_of(file.id)
                projected_path = new_path + current_path[len(old_path):]
                target = TransferTarget(file.parent_id, target_channel, projected_path.rpartition("/")[0])
                source_channel = await fs.effective_channel(file.parent_id)
                parts = tuple(part_record(p) for p in await fs.repo.parts_of(file.id))
                reqs = [AccessRequirement(source_channel, "write"), AccessRequirement(target_channel, "write")]
                plans.append((file.id, target, source_channel, reqs, parts, await self.version(fs, file), current_path, projected_path))
                hook_contexts.append((file.id, file.parent_id, current_path, projected_path))
            await fs.repo.session.commit()
            workers_limit = asyncio.Semaphore(fs._admission.scheduler.concurrent)
            async def stage_member(plan):
                file_id, target, channel, reqs, parts, expected, old, new = plan
                async with workers_limit:
                    async with fs._admission.service._sessions() as session:
                        worker = copy(fs)
                        worker.repo = NodeRepo(session)
                        async with fs._admission.start(reqs, telegram_required=bool(parts)) as ctx:
                            worker._admitted_versions = (*expected.ancestors, *destination_version)
                            await self.assert_versions(worker, worker._admitted_versions)
                            await session.commit()
                            try:
                                prepared = await prepare_transfer(ctx, parts, channel) if parts else PreparedTransfer("", ())
                            except PartAccessDenied as exc:
                                raise FolderMoveSkipped(old, exc) from exc
                            worker._operation_context, worker._executor = ctx, None
                            worker._gateway = ctx.gateway if ctx.member else fs._gateway
                            node = await worker.get(file_id)
                            await worker._run_before_plugin("before_file_move", operation="file_move", node=node,
                                old_parent_id=node.parent_id, new_parent_id=node.parent_id, old_path=old, new_path=new)
                            staged_files.append(await self.stage(worker, node, target, ctx, prepared, move=True, expected=expected))
            tasks = [asyncio.create_task(stage_member(plan)) for plan in plans]
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            staged_files.sort(key=lambda staged: staged.source_id)
            ids = [v[0] for v in original_tree] + [v[0] for v in destination_version]
            await fs.repo.session.execute(select(Node).where(Node.id.in_(ids)).order_by(Node.id).with_for_update())
            current_tree = [await fs.repo.get(root_id), *await fs.repo.subtree(root_id, state="ACTIVE")]
            if tuple(node_version(n) for n in current_tree) != original_tree:
                raise StaleTransfer("Folder tree changed during transfer")
            if await self._tree_version(fs, new_parent_id) != destination_version:
                raise StaleTransfer("Destination changed during folder transfer")
            # Validate every member against the old tree before publishing any.
            for staged in staged_files:
                await self.validate(fs, staged)
            await self._replace(fs, replacement)
            for staged in staged_files:
                await self._publish_move(fs, staged, staged.target.parent_id)
            await fs.repo.set_fields(root_id, parent_id=new_parent_id, name=name or root.name)
            await fs.repo.session.commit()
        except BaseException:
            await fs.repo.session.rollback()
            for staged in staged_files:
                await self.discard(fs, staged)
            raise
        fs.last_warnings = []
        for staged in staged_files:
            await self.cleanup_originals(fs, staged)
            async with staged.admission.scheduler.slot():
                ctx = None
                try:
                    ctx = await self._cleanup_context(staged)
                    worker = copy(fs)
                    worker._operation_context, worker._executor = ctx, None
                    worker._gateway = ctx.gateway if ctx.member else fs._gateway
                    await worker._sync_part_captions(staged.source_id, changed_fields={"path"})
                except Exception as exc:
                    staged.warnings.append(f"Caption synchronization failed: {exc}")
                finally:
                    if ctx is not None and ctx.member:
                        staged.admission.pool.release(ctx.member)
            fs.last_warnings.extend(staged.warnings)
        for file_id, parent_id, old, new in hook_contexts:
            await fs._run_after_plugin("after_file_move", operation="file_move", node=await fs.get(file_id),
                old_parent_id=parent_id, new_parent_id=parent_id, old_path=old, new_path=new)
        return await fs.get(root_id)

    async def _tree_version(self, fs, node_id):
        nodes = [await fs.repo.get(node_id), *await fs.repo.ancestors(node_id)]
        return tuple(node_version(n) for n in nodes if n is not None)

    async def version(self, fs, source):
        return TransferVersion(node_version(source),
            tuple(part_record(p) for p in await fs.repo.parts_of(source.id)),
            await fs.repo.content_of(source.id),
            tuple(node_version(n) for n in await fs.repo.ancestors(source.id)))

    async def stage(self, fs, source, target, context, prepared, *, move=False, name=None, expected=None):
        await self.assert_versions(fs, getattr(fs, "_admitted_versions", ()))
        source = await fs.get(source.id)
        version = await self.version(fs, source)
        if expected is not None and version != expected:
            raise StaleTransfer("Source changed after admission")
        if version.content is None and version.parts != prepared.parts:
            raise StaleTransfer("Source parts changed after preflight")
        target_version = await self._tree_version(fs, target.parent_id)
        source_id, source_mime = source.id, source.mime
        source_info = dict(source.info or {})
        name = name or source.name
        account = context.member.name if context.member else None
        info = {**source_info, "_transfer": {"source_id": source_id, "account_name": account,
                "users": sorted(context.snapshot.user_accounts), "move": move}}
        new = await fs.repo.create(name=name, parent_id=target.parent_id, is_folder=False,
            mime=source_mime, channel_id=target.channel_id, state="TEMP",
            size=source.size, content=version.content, info=info)
        await fs.repo.session.commit()
        staged = StagedTransfer(source_id, version, new.id, target, target_version,
            account, context.snapshot, context.admission, move)
        if version.content is not None:
            return staged

        async def copied(rec):
            staged.new_parts.append(rec)
            await self._persist_part(fs, staged.staging_id, rec)
            await fs.repo.session.commit()

        try:
            records = await channels.forward_parts(context.gateway, prepared.parts, target.channel_id,
                always_copy=not move,
                caption_factory=lambda p: fs._render_part_caption_values(
                    node_id=source_id if move else staged.staging_id,
                    parent_path=target.parent_path, logical_name=name, idx=p.idx,
                    total_parts=len(prepared.parts), part=p, mime=source_mime,
                    info_notes=source_info.get("notes", ""), channel_id=target.channel_id),
                on_copied=copied)
            # Parts retained by same-channel moves are not new owned messages.
            new_keys = {(p.channel_id, p.message_id) for p in staged.new_parts}
            for rec in records:
                if (rec.channel_id, rec.message_id) not in new_keys:
                    await self._persist_part(fs, staged.staging_id, rec)
            if sum(p.size for p in records) != version.node[5]:
                raise StaleTransfer("Part sizes do not match the source file")
            await fs.repo.session.commit()
            return staged
        except BaseException:
            await fs.repo.session.rollback()
            await self.discard(fs, staged, context=context)
            raise

    async def _persist_part(self, fs, file_id, p):
        await fs.repo.add_part(file_id, idx=p.idx, channel_id=p.channel_id,
            message_id=p.message_id, doc_id=p.doc_id, size=p.size,
            original_filename=p.original_filename)

    async def validate(self, fs, staged):
        ids = {staged.source_id, staged.target.parent_id,
               *(v[0] for v in staged.version.ancestors),
               *(v[0] for v in staged.target_version)}
        await fs.repo.session.execute(select(Node).where(Node.id.in_(ids)).order_by(Node.id).with_for_update())
        await fs.repo.session.execute(select(Part).where(Part.file_id == staged.source_id).with_for_update())
        source = await fs.repo.get(staged.source_id)
        if source is None or await self.version(fs, source) != staged.version:
            raise StaleTransfer("Source changed during transfer")
        if await self._tree_version(fs, staged.target.parent_id) != staged.target_version:
            raise StaleTransfer("Destination changed during transfer")

    async def commit_copy(self, fs, staged):
        await self.validate(fs, staged)
        source = await fs.repo.get(staged.source_id)
        await fs.repo.set_fields(staged.staging_id, state="ACTIVE", info=dict(source.info or {}))
        await fs.repo.session.commit()
        return await fs.repo.get(staged.staging_id)

    async def _publish_move(self, fs, staged, new_parent_id):
        staging = await fs.get(staged.staging_id)
        name = staging.name
        records = tuple(part_record(p) for p in await fs.repo.parts_of(staged.staging_id))
        await fs.repo.clear_parts(staged.source_id)
        for p in records:
            await self._persist_part(fs, staged.source_id, p)
        await fs.repo.set_fields(staged.source_id, parent_id=new_parent_id, name=name, channel_id=staged.target.channel_id)
        await fs.repo.purge(staged.staging_id)

    async def commit_move(self, fs, staged, new_parent_id):
        await self.validate(fs, staged)
        await self._publish_move(fs, staged, new_parent_id)
        await fs.repo.session.commit()
        return await fs.repo.get(staged.source_id)

    async def _cleanup_context(self, staged):
        from tgshelf.core.operations import OperationContext, recovery_wait
        admission = staged.admission
        names = {staged.account_name} if staged.account_name else set()
        while names:
            member = admission.pool.replace(allowed_names=names)
            if member is not None:
                admission.pool.acquire(member)
                return OperationContext(admission, staged.snapshot, member)
            wait = recovery_wait(admission.pool, names)
            if wait is None:
                raise RuntimeError("Transfer cleanup account unavailable")
            await admission.sleep(max(wait, 0))
        return OperationContext(admission, staged.snapshot, None)

    async def discard(self, fs, staged, *, context=None):
        # Retained same-channel originals belong exclusively to the ACTIVE
        # source. Recovery metadata must never make them purgeable as staging.
        if await fs.get(staged.staging_id) is None:
            return
        await fs.repo.clear_parts(staged.staging_id)
        for p in staged.new_parts:
            await self._persist_part(fs, staged.staging_id, p)
        await fs.repo.session.commit()
        if context is None:
            async with staged.admission.scheduler.slot():
                try:
                    ctx = await self._cleanup_context(staged)
                except Exception as exc:
                    log.error("[transfer] staging retained node=%s: %s", staged.staging_id, exc)
                    return
                try:
                    return await self.discard(fs, staged, context=ctx)
                finally:
                    if ctx.member:
                        staged.admission.pool.release(ctx.member)
        failed_parts = []
        for p in staged.new_parts:
            try:
                await context.gateway.delete_message(p.channel_id, p.message_id)
            except Exception as exc:
                failed_parts.append(p)
                log.error("[transfer] staging cleanup failed node=%s channel=%s message=%s: %s",
                          staged.staging_id, p.channel_id, p.message_id, exc)
        if not failed_parts:
            await fs.repo.purge(staged.staging_id)
            await fs.repo.session.commit()
        else:
            # A failed callback/commit may have rolled back the only part row.
            # Retry recording known accepted IDs while the DB is available.
            await fs.repo.clear_parts(staged.staging_id)
            for p in failed_parts:
                await self._persist_part(fs, staged.staging_id, p)
            await fs.repo.session.commit()

    async def cleanup_originals(self, fs, staged, context=None):
        if context is None:
            async with staged.admission.scheduler.slot():
                try:
                    ctx = await self._cleanup_context(staged)
                except Exception as exc:
                    warning = f"Original cleanup account unavailable: {exc}"
                    staged.warnings.append(warning)
                    log.error("[transfer] %s", warning)
                    return
                try:
                    return await self.cleanup_originals(fs, staged, ctx)
                finally:
                    if ctx.member:
                        staged.admission.pool.release(ctx.member)
        for p in staged.version.parts:
            if p.channel_id == staged.target.channel_id:
                continue
            try:
                await context.gateway.delete_message(p.channel_id, p.message_id)
            except Exception as exc:
                warning = f"Original cleanup failed: channel={p.channel_id} message={p.message_id}: {exc}"
                staged.warnings.append(warning)
                log.error("[transfer] %s", warning)
