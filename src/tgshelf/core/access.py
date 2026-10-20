"""Live folder-channel policy; immutable candidates outlive the short DB read."""
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from tgshelf.config import AccountConfig
from tgshelf.db.access import ChannelAccessRepo
from tgshelf.db.repo import NodeRepo

AccessMode = Literal["none", "read", "write"]


class NoAuthorizedAccount(Exception):
    code = "no_authorized_account"


class LockedChannelAccess(PermissionError):
    """The master channel policy is fixed and cannot be edited."""


@dataclass(frozen=True)
class AccessRequirement:
    channel_id: int
    mode: Literal["read", "write"]


@dataclass(frozen=True)
class AccessSnapshot:
    requirements: tuple[AccessRequirement, ...]
    user_accounts: frozenset[str]
    bot_accounts: frozenset[str]


@dataclass(frozen=True)
class AccessUpdate:
    account_name: str
    mode: AccessMode | None


class ChannelAccessService:
    def __init__(self, session_factory, accounts: Sequence[AccountConfig], master_channel: int):
        self._sessions = session_factory
        self._accounts = tuple(accounts)
        self.master_channel = master_channel

    def _mode(self, account, channel, overrides):
        mode = "write" if channel == self.master_channel else overrides.get((account.name, channel), "write")
        return "read" if account.is_bot and mode == "write" else mode

    async def snapshot(self, requirements: Sequence[AccessRequirement], *, allow_bots=False) -> AccessSnapshot:
        return await self._snapshot(requirements, allow_bots=allow_bots)

    async def snapshot_independent(self, requirements: Sequence[AccessRequirement]) -> AccessSnapshot:
        """Authorize DB-only tree changes without requiring one common writer."""
        return await self._snapshot(requirements, independent=True)

    async def _snapshot(self, requirements, *, allow_bots=False, independent=False):
        modes = {}
        for req in requirements:
            if req.mode not in ("read", "write"):
                raise ValueError("Invalid access requirement")
            modes[req.channel_id] = "write" if req.mode == "write" or modes.get(req.channel_id) == "write" else "read"
        normalized = tuple(AccessRequirement(c, m) for c, m in sorted(modes.items()))
        async with self._sessions() as session:
            rows = await ChannelAccessRepo(session).get_many(list(modes))
            overrides = {(r.account_name, r.channel_id): r.mode for r in rows}
        users, bots = set(), set()
        covered = set()
        for account in self._accounts:
            if account.is_bot and not allow_bots:
                continue
            if account.is_bot and any(r.mode == "write" for r in normalized):
                continue
            matches = [r.channel_id for r in normalized if self._mode(account, r.channel_id, overrides) in
                       (("read", "write") if r.mode == "read" else ("write",))]
            covered.update(matches)
            if (independent and matches) or len(matches) == len(normalized):
                (bots if account.is_bot else users).add(account.name)
        if independent and covered != set(modes):
            raise NoAuthorizedAccount("At least one folder has no authorized user account")
        if not users and not bots:
            raise NoAuthorizedAccount("No configured account has the required folder access")
        return AccessSnapshot(normalized, frozenset(users), frozenset(bots))

    async def _grid(self, session):
        repo = NodeRepo(session)
        folders = await repo.explicit_channel_folders()
        channels = sorted({f.channel_id for f in folders})
        rows = await ChannelAccessRepo(session).get_many(channels)
        overrides = {(r.account_name, r.channel_id): r.mode for r in rows}
        return {
            "accounts": [{"name": a.name, "kind": "bot" if a.is_bot else "user"} for a in self._accounts],
            "folders": [{"id": f.id, "path": await repo.path_of(f.id), "channel_id": f.channel_id} for f in folders],
            "permissions": [dict(account_name=a.name, channel_id=c, mode=self._mode(a, c, overrides),
                                 is_default=c == self.master_channel or (a.name, c) not in overrides,
                                 is_locked=c == self.master_channel)
                            for c in channels for a in self._accounts],
        }

    async def list_grid(self) -> dict:
        async with self._sessions() as session:
            return await self._grid(session)

    async def update(self, channel_id: int, updates: Sequence[AccessUpdate]) -> dict:
        if channel_id == self.master_channel:
            raise LockedChannelAccess("Root/master access cannot be changed")
        accounts = {a.name: a for a in self._accounts}
        names = set()
        for u in updates:
            if u.account_name in names or u.account_name not in accounts:
                raise ValueError("Unknown or duplicate account")
            if u.mode not in (None, "none", "read", "write"):
                raise ValueError("Invalid access mode")
            if accounts[u.account_name].is_bot and u.mode == "write":
                raise ValueError("Bots cannot have write access")
            names.add(u.account_name)
        async with self._sessions() as session:
            if channel_id not in {f.channel_id for f in await NodeRepo(session).explicit_channel_folders()}:
                raise ValueError("Channel is not mapped to an active folder")
            await ChannelAccessRepo(session).set_many(channel_id, {u.account_name: u.mode for u in updates})
            grid = await self._grid(session)
            await session.commit()
            return grid
