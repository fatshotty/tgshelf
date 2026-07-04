"""Canonical Telegram captions for tgshelf-owned file messages."""

from __future__ import annotations


def logical_part_filename(name: str, *, idx: int, total_parts: int) -> str:
    if total_parts <= 1:
        return name
    return f"{name}.{idx + 1:03d}"


def logical_part_caption(name: str, *, idx: int, total_parts: int) -> str:
    return f"fileName: {logical_part_filename(name, idx=idx, total_parts=total_parts)}"


def caption_first_line_filename(caption: str | None) -> str | None:
    if not isinstance(caption, str):
        return None
    lines = caption.splitlines()
    first_line = lines[0] if lines else ""
    key, sep, value = first_line.partition(":")
    if sep and key.strip() == "fileName":
        filename = value.strip()
        return filename or None
    return None
