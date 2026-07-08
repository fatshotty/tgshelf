"""Canonical Telegram captions for tgshelf-owned file messages."""

from __future__ import annotations

import string
from dataclasses import dataclass

DEFAULT_CAPTION_TEMPLATE = "fileName: {filename}"
CAPTION_PLACEHOLDERS = (
    "id",
    "path",
    "filename",
    "part_idx",
    "parts",
    "size",
    "mime",
    "channel_id",
    "info",
)
RESERVED_CAPTION_PLACEHOLDERS = ()


@dataclass(frozen=True)
class CaptionRenderContext:
    node_id: str
    parent_path: str
    logical_name: str
    idx: int
    total_parts: int
    part_size: int
    mime: str | None
    channel_id: int
    info_notes: str = ""


def logical_part_filename(name: str, *, idx: int, total_parts: int) -> str:
    if total_parts <= 1:
        return name
    return f"{name}.{idx + 1:03d}"


def render_caption(template: str, context: CaptionRenderContext) -> str:
    if template == "":
        return ""
    values = {
        "id": context.node_id,
        "path": context.parent_path,
        "filename": logical_part_filename(
            context.logical_name, idx=context.idx, total_parts=context.total_parts
        ),
        "part_idx": str(context.idx + 1),
        "parts": str(context.total_parts),
        "size": str(context.part_size),
        "mime": context.mime or "",
        "channel_id": str(context.channel_id),
        "info": context.info_notes,
    }
    return template.format(**values)


def logical_part_caption(
    name: str, *, idx: int, total_parts: int, template: str = DEFAULT_CAPTION_TEMPLATE
) -> str:
    return render_caption(
        template,
        CaptionRenderContext(
            node_id="",
            parent_path="",
            logical_name=name,
            idx=idx,
            total_parts=total_parts,
            part_size=0,
            mime=None,
            channel_id=0,
        ),
    )


def caption_template_fields(template: str) -> frozenset[str]:
    fields: set[str] = set()
    for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(template):
        if field_name is not None:
            fields.add(field_name)
    return frozenset(fields)


def caption_template_depends_on(template: str, changed: set[str]) -> bool:
    if template == "":
        return False
    fields = caption_template_fields(template)
    if fields & changed:
        return True
    if "filename" in fields and {"part_idx", "parts"} & changed:
        return True
    return False


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
