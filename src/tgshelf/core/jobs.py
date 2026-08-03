"""Best-effort, durable execution of Web UI bulk filesystem operations."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Literal, Sequence

from tgshelf.db.ids import generate_node_id
from tgshelf.db.jobs import OperationJobRepo
from tgshelf.db.models import OperationJob, OperationJobItem

log = logging.getLogger("tgshelf.jobs")

OperationKind = Literal["move", "delete"]


class OperationJobService:
    def __init__(self, session_factory, fs_factory, *, cleanup_after: timedelta = timedelta(days=30)):
        self._session_factory = session_factory
        self._fs_factory = fs_factory
        self._cleanup_after = cleanup_after
        self._tasks: dict[str, asyncio.Task] = {}
        self._cleanup_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def create(
        self, operation: OperationKind, node_ids: Sequence[str], parent_id: str | None = None
    ) -> OperationJob:
        if operation not in ("move", "delete"):
            raise ValueError("operation must be move or delete")
        unique_ids = tuple(dict.fromkeys(node_ids))
        if not unique_ids:
            raise ValueError("node_ids must be non-empty")
        if operation == "move" and not parent_id:
            raise ValueError("parent_id is required for move")
        if operation == "delete" and parent_id is not None:
            raise ValueError("parent_id is only valid for move")

        async with self._session_factory() as session:
            fs = self._fs_factory(session)
            if parent_id is not None:
                await fs.ensure_move_target(parent_id)

            nodes = {node_id: await fs.get(node_id) for node_id in unique_ids}
            selected_folders = {
                node_id for node_id, node in nodes.items()
                if node is not None and node.is_folder and node.state == "ACTIVE"
            }
            items: list[OperationJobItem] = []
            now = datetime.now().astimezone()
            for position, node_id in enumerate(unique_ids):
                node = nodes[node_id]
                source_name = node.name if node is not None else None
                source_path = await fs.path_of(node_id) if node is not None else None
                covered = False
                if node is not None:
                    ancestors = await fs.repo.ancestors(node_id)
                    covered = any(ancestor.id in selected_folders for ancestor in ancestors)
                items.append(
                    OperationJobItem(
                        job_id="",
                        position=position,
                        node_id=node_id,
                        source_name=source_name,
                        source_path=source_path,
                        state="skipped" if covered else "pending",
                        error="covered by a selected ancestor folder" if covered else None,
                        finished_at=now if covered else None,
                    )
                )

            job_id = generate_node_id()
            job = OperationJob(
                id=job_id,
                operation=operation,
                parent_id=parent_id,
                total=len(items),
                skipped=sum(item.state == "skipped" for item in items),
            )
            for item in items:
                item.job_id = job_id
            await OperationJobRepo(session).create(job, items)

        self._tasks[job_id] = asyncio.create_task(self._run(job_id))
        return job

    async def get(self, job_id: str):
        async with self._session_factory() as session:
            repo = OperationJobRepo(session)
            job = await repo.get(job_id)
            return (job, await repo.items(job_id)) if job is not None else None

    async def list(self, *, state: str | None = None, limit: int = 50, offset: int = 0):
        async with self._session_factory() as session:
            return await OperationJobRepo(session).list(
                state=state, limit=max(1, min(limit, 100)), offset=max(0, offset)
            )

    async def recover(self) -> int:
        async with self._session_factory() as session:
            return await OperationJobRepo(session).interrupt_unfinished(
                "server restarted before job completion"
            )

    async def cleanup(self) -> int:
        async with self._session_factory() as session:
            return await OperationJobRepo(session).cleanup_terminal_before(
                datetime.now().astimezone() - self._cleanup_after
            )

    async def aclose(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(24 * 60 * 60)
            try:
                removed = await self.cleanup()
                if removed:
                    log.info("[job] cleanup_removed=%d", removed)
            except Exception:  # noqa: BLE001 - retry cleanup on the next interval
                log.exception("[job] cleanup_failed")

    async def _run(self, job_id: str) -> None:
        try:
            async with self._session_factory() as session:
                repo = OperationJobRepo(session)
                job = await repo.get(job_id)
                if job is None:
                    return
                await repo.mark_job_running(job_id)
                items = await repo.items(job_id)

            log.info("[job] job_id=%s operation=%s state=started", job_id, job.operation)
            for item in items:
                if item.state != "pending":
                    continue
                async with self._session_factory() as session:
                    repo = OperationJobRepo(session)
                    await repo.mark_item_running(job_id, item.position)
                try:
                    async with self._session_factory() as session:
                        fs = self._fs_factory(session)
                        node = await fs.get(item.node_id)
                        if node is None:
                            raise ValueError(f"node {item.node_id} not found")
                        if job.operation == "move":
                            await fs.move(item.node_id, job.parent_id)
                        else:
                            await fs.delete(item.node_id, purge=False)
                    async with self._session_factory() as session:
                        await OperationJobRepo(session).finish_item(job_id, item.position, "succeeded")
                    log.info("[job] job_id=%s node_id=%s item_state=succeeded", job_id, item.node_id)
                except Exception as exc:  # noqa: BLE001 - one item must not stop the job
                    async with self._session_factory() as session:
                        await OperationJobRepo(session).finish_item(
                            job_id, item.position, "failed", str(exc)
                        )
                    log.warning(
                        "[job] job_id=%s node_id=%s item_state=failed error=%s",
                        job_id, item.node_id, exc,
                    )

            async with self._session_factory() as session:
                repo = OperationJobRepo(session)
                await repo.finish_job(job_id, "completed")
                finished = await repo.get(job_id)
            log.info(
                "[job] job_id=%s state=completed succeeded=%d failed=%d skipped=%d",
                job_id, finished.succeeded, finished.failed, finished.skipped,
            )
        except asyncio.CancelledError:
            async with self._session_factory() as session:
                await OperationJobRepo(session).interrupt(job_id, "server shutdown before job completion")
            raise
        except Exception as exc:  # noqa: BLE001 - persist runner-level failure
            async with self._session_factory() as session:
                repo = OperationJobRepo(session)
                await repo.interrupt(job_id, f"runner failed: {exc}")
                await repo.finish_job(job_id, "failed", str(exc))
            log.exception("[job] job_id=%s state=failed", job_id)
        finally:
            self._tasks.pop(job_id, None)
