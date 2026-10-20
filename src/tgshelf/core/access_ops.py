"""Admission wrappers for facade entry points, including internal mirror calls."""
from copy import copy
from functools import wraps
from inspect import signature

from tgshelf.constants import ROOT_ID
from tgshelf.core.access import AccessRequirement, NoAuthorizedAccount
from tgshelf.core.transfer import TransferService, node_version, prepare_transfer


def managed(kind):
    def decorate(method):
        sig = signature(method)
        @wraps(method)
        async def guarded(self, *args, **kwargs):
            if self._admission is None:
                return await method(self, *args, **kwargs)
            params = sig.bind(self, *args, **kwargs).arguments
            reqs, telegram, parts, independent, expected = [], False, (), False, None
            watched = set()
            replacement = ()
            if kind == "copy":
                source = params["src"]
                watched.update((source.id, params["dst_parent_id"]))
                channel = await self.effective_channel(source.parent_id)
                reqs = [AccessRequirement(channel, "read"),
                        AccessRequirement(await self.effective_channel(params["dst_parent_id"]), "write")]
                parts = tuple(await self.repo.parts_of(source.id))
                expected = await TransferService().version(self, source)
                telegram = bool(parts)
                if "old_dest_id" in params:
                    replacement = await TransferService()._replacement(self, params["old_dest_id"])
            elif kind in ("write", "mkdir"):
                watched.add(params["parent_id"])
                reqs = [AccessRequirement(await self.effective_channel(params["parent_id"]), "write")]
                if kind == "write" and params.get("overwrite", False):
                    old = await self.repo.get_child_by_name(params["parent_id"], params["name"], state="ACTIVE")
                    replacement = await TransferService()._replacement(self, old.id if old else None)
                # Upload probing determines whether Telegram is needed, without
                # requiring a healthy session for inline content.
            elif kind == "import":
                reqs = [AccessRequirement(await self.effective_channel(params.get("parent_id", ROOT_ID)), "read")]
                telegram = True
            elif kind == "merge":
                for node_id in [params["target_id"], *params["donor_ids"]]:
                    watched.add(node_id)
                    node = await self.repo.get(node_id)
                    if node is not None:
                        reqs.append(AccessRequirement(await self.effective_channel(node.parent_id), "write"))
                        telegram |= bool(await self.repo.parts_of(node_id))
            else:
                node_id = params.get("node_id", params.get("file_id"))
                node = await self.repo.get(node_id)
                if node is None:
                    return await method(self, *args, **kwargs)
                if kind == "delete" and params.get("purge", False) and node.is_folder and self._operation_context is None:
                    return await purge_tree(self, node_id, deleted_only=params.get("deleted_only", False))
                nodes = [node]
                if node.is_folder:
                    nodes.extend(await self.repo.subtree(node.id, state=None))
                for n in nodes:
                    watched.add(n.id)
                    folder = n.id if n.is_folder else n.parent_id
                    reqs.append(AccessRequirement(await self.effective_channel(folder), "write"))
                    if not n.is_folder and kind not in ("state", "inline"):
                        telegram |= bool(await self.repo.parts_of(n.id))
                if (kind == "delete" and not params.get("purge", False)) or (node.is_folder and kind == "mutation"):
                    telegram = False
                independent = node.is_folder and not telegram
            extra = await TransferService().replacement_requirements(self, replacement)
            if self._operation_context is not None:
                admitted = {r.channel_id: r.mode for r in self._operation_context.snapshot.requirements}
                if any(r.channel_id not in admitted or
                       (r.mode == "write" and admitted[r.channel_id] != "write") for r in [*reqs, *extra]):
                    raise NoAuthorizedAccount("Nested mutation exceeds the admitted folder access")
                return await method(self, *args, **kwargs)
            versions = {}
            for node_id in watched:
                node = await self.repo.get(node_id)
                for n in ([node] if node else []) + list(await self.repo.ancestors(node_id)):
                    versions[n.id] = node_version(n)
            # All preceding operations are metadata reads. End the transaction
            # before policy/health/slot waits; commit preserves detached values.
            await self.repo.session.commit()
            async with self._admission.start(reqs, telegram_required=telegram, independent=independent,
                                             extra_requirements=extra) as ctx:
                await TransferService().assert_versions(self, (*versions.values(), *replacement))
                await self.repo.session.commit()
                worker = copy(self)
                worker._operation_context = ctx
                worker._executor = None
                worker._latest_staged = None
                worker._expected_transfer = expected
                worker._replacement_versions = replacement
                worker._admitted_versions = tuple(versions.values())
                worker.last_warnings = []
                if ctx.member is not None:
                    worker._gateway = ctx.gateway
                if kind == "copy" and parts:
                    worker._prepared_transfer = await prepare_transfer(ctx, parts, channel)
                    worker._gateway = ctx.gateway
                    await TransferService().assert_versions(self, versions.values())
                await self.repo.session.commit()
                try:
                    result = await method(worker, *args, **kwargs)
                    self.last_account_name = ctx.member.name if ctx.member else None
                    self.last_warnings = worker.last_warnings
                    return result
                except BaseException:
                    await self.repo.session.rollback()
                    if worker._latest_staged is not None:
                        staged_node = await self.repo.get(worker._latest_staged.staging_id)
                        if staged_node is not None and staged_node.state == "TEMP":
                            await TransferService().discard(worker, worker._latest_staged, context=ctx)
                    raise
        return guarded
    return decorate


async def purge_tree(fs, root_id, *, deleted_only=False):
    """Purge per file; never drop failed members' only remaining references."""
    from sqlalchemy import select
    from tgshelf.db.models import Node
    tree = [await fs.get(root_id), *await fs.repo.subtree(root_id, state=None)]
    files = [n.id for n in tree if n is not None and not n.is_folder
             and (not deleted_only or n.state == "DELETED")]
    folders = [n.id for n in reversed(tree) if n is not None and n.is_folder
               and n.id != ROOT_ID and (not deleted_only or n.state == "DELETED")]
    await fs.repo.session.commit()
    failures = []
    for file_id in files:
        try:
            await fs.delete(file_id, purge=True, deleted_only=deleted_only)
        except Exception as exc:
            await fs.repo.session.rollback()
            failures.append(exc)
    for folder_id in folders:
        try:
            channel = await fs.effective_channel(folder_id)
            await fs.repo.session.commit()
            async with fs._admission.start([AccessRequirement(channel, "write")], telegram_required=False):
                await fs.repo.session.execute(select(Node).where(Node.id == folder_id).with_for_update())
                if not await fs.repo.children(folder_id, state=None):
                    await fs.repo.purge(folder_id)
                await fs.repo.session.commit()
        except Exception as exc:
            await fs.repo.session.rollback()
            failures.append(exc)
    if failures:
        raise failures[0]
