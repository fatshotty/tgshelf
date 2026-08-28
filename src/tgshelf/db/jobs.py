"""Persistence primitives for durable Web UI operation jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tgshelf.db.models import OperationJob, OperationJobItem


class OperationJobRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, job: OperationJob, items: Sequence[OperationJobItem]) -> None:
        self.session.add(job)
        self.session.add_all(items)
        await self.session.commit()

    async def get(self, job_id: str) -> OperationJob | None:
        return await self.session.get(OperationJob, job_id)

    async def list(self, *, state: str | None, limit: int, offset: int) -> Sequence[OperationJob]:
        stmt = (
            select(OperationJob)
            .order_by(OperationJob.created_at.desc(), OperationJob.id.desc())
            .offset(offset)
            .limit(limit)
        )
        if state is not None:
            stmt = stmt.where(OperationJob.state == state)
        return (await self.session.execute(stmt)).scalars().all()

    async def items(self, job_id: str) -> Sequence[OperationJobItem]:
        result = await self.session.execute(
            select(OperationJobItem)
            .where(OperationJobItem.job_id == job_id)
            .order_by(OperationJobItem.position)
        )
        return result.scalars().all()

    async def mark_job_running(self, job_id: str) -> None:
        job = await self._required_job(job_id)
        job.state = "running"
        job.started_at = datetime.now().astimezone()
        await self.session.commit()

    async def mark_item_running(self, job_id: str, position: int) -> None:
        item = await self._required_item(job_id, position)
        item.state = "running"
        item.started_at = datetime.now().astimezone()
        await self.session.commit()

    async def finish_item(
        self, job_id: str, position: int, state: str, error: str | None = None
    ) -> None:
        if state not in ("succeeded", "failed", "skipped"):
            raise ValueError(f"invalid terminal job item state: {state}")
        item = await self._required_item(job_id, position)
        job = await self._required_job(job_id)
        item.state = state
        item.error = error
        item.finished_at = datetime.now().astimezone()
        setattr(job, state, getattr(job, state) + 1)
        await self.session.commit()

    async def finish_job(self, job_id: str, state: str, error: str | None = None) -> None:
        if state not in ("completed", "failed", "interrupted"):
            raise ValueError(f"invalid terminal job state: {state}")
        job = await self._required_job(job_id)
        job.state = state
        job.error = error
        job.finished_at = datetime.now().astimezone()
        await self.session.commit()

    async def interrupt_unfinished(self, reason: str) -> int:
        result = await self.session.execute(
            select(OperationJob).where(OperationJob.state.in_(("queued", "running")))
        )
        jobs = result.scalars().all()
        for job in jobs:
            await self._interrupt(job, reason)
        if jobs:
            await self.session.commit()
        return len(jobs)

    async def interrupt(self, job_id: str, reason: str) -> None:
        await self._interrupt(await self._required_job(job_id), reason)
        await self.session.commit()

    async def _interrupt(self, job: OperationJob, reason: str) -> None:
        if job.state not in ("queued", "running"):
            return
        now = datetime.now().astimezone()
        items = await self.items(job.id)
        pending = [item for item in items if item.state in ("pending", "running")]
        for item in pending:
            item.state = "skipped"
            item.error = reason
            item.finished_at = now
        job.skipped += len(pending)
        job.state = "interrupted"
        job.error = reason
        job.finished_at = now

    async def cleanup_terminal_before(self, cutoff: datetime) -> int:
        result = await self.session.execute(
            select(OperationJob.id).where(
                OperationJob.state.in_(("completed", "failed", "interrupted")),
                OperationJob.finished_at.is_not(None),
                OperationJob.finished_at < cutoff,
            )
        )
        job_ids = list(result.scalars())
        if job_ids:
            await self.session.execute(delete(OperationJob).where(OperationJob.id.in_(job_ids)))
            await self.session.commit()
        return len(job_ids)

    async def _required_job(self, job_id: str) -> OperationJob:
        job = await self.get(job_id)
        if job is None:
            raise ValueError(f"operation job {job_id} not found")
        return job

    async def _required_item(self, job_id: str, position: int) -> OperationJobItem:
        item = await self.session.get(OperationJobItem, {"job_id": job_id, "position": position})
        if item is None:
            raise ValueError(f"operation job item {job_id}:{position} not found")
        return item
