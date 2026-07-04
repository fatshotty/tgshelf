"""JSON serialization for API responses. Clean JSON (no legacy compatibility)."""

from __future__ import annotations

from typing import Any

from tgshelf.db.models import Node


def node_to_dict(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.name,
        "is_folder": node.is_folder,
        "mime": node.mime,
        "channel_id": node.channel_id,
        "state": node.state,
        "size": node.size,
        "inline": bool(node.inline),
        "ctime": node.ctime.isoformat() if node.ctime else None,
        "mtime": node.mtime.isoformat() if node.mtime else None,
        "info": node.info,
    }
