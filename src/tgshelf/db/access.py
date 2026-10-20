"""Transactional account/channel policy persistence; callers own commit/rollback."""
from collections.abc import Mapping, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tgshelf.db.models import ChannelAccess


class ChannelAccessRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_many(self, channel_ids: Sequence[int]) -> Sequence[ChannelAccess]:
        if not channel_ids:
            return []
        result = await self.session.scalars(
            select(ChannelAccess).where(ChannelAccess.channel_id.in_(set(channel_ids)))
        )
        return list(result.all())

    async def set_many(self, channel_id: int, updates: Mapping[str, str | None]) -> None:
        resets = [name for name, mode in updates.items() if mode is None]
        if resets:
            await self.session.execute(delete(ChannelAccess).where(
                ChannelAccess.channel_id == channel_id,
                ChannelAccess.account_name.in_(resets),
            ))
        values = [dict(account_name=name, channel_id=channel_id, mode=mode)
                  for name, mode in updates.items() if mode is not None]
        if values:
            stmt = insert(ChannelAccess).values(values)
            await self.session.execute(stmt.on_conflict_do_update(
                index_elements=["account_name", "channel_id"],
                set_={"mode": stmt.excluded.mode, "updated_at": func.now()},
            ))
