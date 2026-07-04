"""Telegram session persistence, two interchangeable backends.

The StringSession of each account is stored either in the DB (`tg_sessions`)
or on a local file (`{data}/{name}.session`), selected by `session_storage`.

Why file matters: the system uses ONE shared DB, but the tool can run on
SEVERAL servers with the same config. A Telethon session must not be used by
two processes at once (AUTH_KEY_DUPLICATED, mutual logouts), so each instance
keeps its own sessions on its local disk via the file backend.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tgshelf.db.models import TgSession

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class SessionStore(Protocol):
    async def load(self, name: str) -> str | None: ...
    async def save(self, name: str, session_string: str, **metadata) -> None: ...
    async def delete(self, name: str) -> None: ...


class FileSessionStore:
    """One `{data}/{name}.session` file per account (legacy-compatible path)."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def _path(self, name: str) -> Path:
        if not _SAFE_NAME.match(name):
            raise ValueError(f"unsafe session name: {name!r}")
        return self.data_dir / f"{name}.session"

    async def load(self, name: str) -> str | None:
        path = self._path(name)
        return await asyncio.to_thread(
            lambda: path.read_text().strip() if path.exists() else None
        )

    async def save(self, name: str, session_string: str, **metadata) -> None:
        path = self._path(name)

        def _write() -> None:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(session_string)

        await asyncio.to_thread(_write)

    async def delete(self, name: str) -> None:
        path = self._path(name)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))


class DbSessionStore:
    """StringSession in the `tg_sessions` table (single-instance deployments)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def load(self, name: str) -> str | None:
        result = await self.session.execute(
            select(TgSession.session_string).where(TgSession.name == name)
        )
        return result.scalar_one_or_none()

    async def save(
        self,
        name: str,
        session_string: str,
        *,
        kind: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        bot_token: str | None = None,
        dc_id: int | None = None,
        is_premium: bool | None = None,
    ) -> None:
        # full upsert when metadata is supplied (first save / re-login),
        # otherwise a narrow update of just the session string
        existing = await self.session.get(TgSession, name)
        if existing is None:
            if kind is None or api_id is None or api_hash is None:
                raise ValueError(
                    f"new session '{name}' requires kind/api_id/api_hash metadata"
                )
            stmt = pg_insert(TgSession).values(
                name=name,
                kind=kind,
                api_id=api_id,
                api_hash=api_hash,
                bot_token=bot_token,
                session_string=session_string,
                dc_id=dc_id,
                is_premium=bool(is_premium) if is_premium is not None else False,
            )
            await self.session.execute(stmt)
        else:
            values: dict = {"session_string": session_string}
            for key, value in (
                ("kind", kind),
                ("api_id", api_id),
                ("api_hash", api_hash),
                ("bot_token", bot_token),
                ("dc_id", dc_id),
                ("is_premium", is_premium),
            ):
                if value is not None:
                    values[key] = value
            await self.session.execute(
                update(TgSession).where(TgSession.name == name).values(**values)
            )
        await self.session.commit()

    async def delete(self, name: str) -> None:
        await self.session.execute(delete(TgSession).where(TgSession.name == name))
        await self.session.commit()


def build_session_store(
    storage: str, *, data_dir: Path, session: AsyncSession | None
) -> SessionStore:
    if storage == "file":
        return FileSessionStore(data_dir)
    if storage == "db":
        if session is None:
            raise ValueError("db session storage requires a database session")
        return DbSessionStore(session)
    raise ValueError(f"unknown session_storage: {storage!r}")
