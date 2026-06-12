"""Changes feed management.

The feed itself is populated by DB triggers (revision 0001) so it is atomic
with the data changes and covers every writer (all tool instances, standalone
scripts, manual fixes). This module only manages its lifecycle:

- `apply_changes_feed_state`: called at startup with `changes_feed.enabled`
  from config — DISABLE TRIGGER means zero overhead when the feed is unused;
  after re-enabling, consumers must do a one-off full resync (events emitted
  while disabled are lost by design).
- `purge_old_changes`: retention job, deletes events older than
  `changes_feed.retention_days`.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_TRIGGERS = (("nodes", "trg_nodes_changes"), ("parts", "trg_parts_changes"))


async def apply_changes_feed_state(conn: AsyncConnection, *, enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    for table, trigger in _TRIGGERS:
        await conn.execute(text(f"ALTER TABLE {table} {action} TRIGGER {trigger}"))


async def purge_old_changes(conn: AsyncConnection, *, retention_days: int) -> int:
    """Delete feed rows older than the retention window; returns rows purged."""
    result = await conn.execute(
        text("DELETE FROM changes WHERE at < now() - make_interval(days => :days)"),
        {"days": retention_days},
    )
    return result.rowcount
