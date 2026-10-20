"""Materialized read metadata and permission candidates, without a DB session."""
from dataclasses import dataclass
from datetime import datetime

from tgshelf.core.access import AccessSnapshot
from tgshelf.core.upload import PartRecord


@dataclass(frozen=True)
class ReadSnapshot:
    node_id: str
    size: int
    channel_id: int
    parts: tuple[PartRecord, ...]
    content: bytes | None
    access: AccessSnapshot
    name: str
    mime: str | None
    mtime: datetime

    @property
    def id(self):
        return self.node_id
