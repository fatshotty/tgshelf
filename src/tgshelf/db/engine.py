"""Async engine and session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(db_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(db_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay usable after commit, no implicit
    # refresh round-trips in the middle of streaming/upload flows
    return async_sessionmaker(engine, expire_on_commit=False)
