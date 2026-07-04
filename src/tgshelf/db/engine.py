"""Async engine and session factory."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(db_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(db_url, echo=echo, pool_pre_ping=True)


async def check_connection(engine: AsyncEngine) -> None:
    """Open one real connection and run ``SELECT 1``.

    ``create_async_engine`` is lazy — it never connects until the first query, so
    a dead/misconfigured DB would otherwise stay invisible until the first
    request. Call this at startup to fail fast with the driver's actual error.
    """
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay usable after commit, no implicit
    # refresh round-trips in the middle of streaming/upload flows
    return async_sessionmaker(engine, expire_on_commit=False)
