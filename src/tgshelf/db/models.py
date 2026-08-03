"""SQLAlchemy models — schema as per PLAN.md (Alembic revision 0001).

Invariants encoded here (and pinned by tests/unit/test_models.py):
- sibling-name uniqueness is case-insensitive and applies to ACTIVE nodes only
  (soft-deleted/TEMP rows may share the name, the partial index frees it);
- parts follow their file (FK CASCADE) and carry channel_id PER PART so an
  interrupted cross-channel move stays consistent and resumable;
- nodes.content is deferred: listings never drag inline file bodies along;
- changes is the append-only feed populated by a DB trigger (revision 0001),
  consumers catch up with WHERE seq > last_seen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, column_property, mapped_column

from tgshelf.constants import ROOT_ID

STATES = ("ACTIVE", "TEMP", "DELETED")
CHANGE_OPS = ("CREATE", "UPDATE", "MOVE", "DELETE")
JOB_OPERATIONS = ("move", "delete")
JOB_STATES = ("queued", "running", "completed", "failed", "interrupted")
JOB_ITEM_STATES = ("pending", "running", "succeeded", "failed", "skipped")


class Base(DeclarativeBase):
    pass


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_folder: Mapped[bool] = mapped_column(Boolean, nullable=False)
    mime: Mapped[str | None] = mapped_column(Text)
    # folder channel override, or current channel of a file; NULL = inherited
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="ACTIVE")
    # denormalized total size (sum of parts, or len(content))
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # inline body for files <= min_size; deferred so listings never load it
    content: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ctime: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    mtime: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("state IN ('ACTIVE', 'TEMP', 'DELETED')", name="ck_nodes_state"),
        CheckConstraint(
            f"id = '{ROOT_ID}' OR parent_id IS NOT NULL",
            name="ck_nodes_root_only_null_parent",
        ),
        Index("ix_nodes_parent_id", "parent_id"),
        Index(
            "uq_nodes_parent_lower_name_active",
            "parent_id",
            func.lower(text("name")),
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
        Index(
            "ix_nodes_lower_name",
            func.lower(text("name")),
            postgresql_ops={"lower": "text_pattern_ops"},
        ),
        Index(
            "ix_nodes_channel_id",
            "channel_id",
            postgresql_where=text("channel_id IS NOT NULL"),
        ),
    )


# Computed (not stored): True when an inline body is present, i.e. the file is
# stored in the DB rather than backed by Telegram parts (content is NULL there).
# A SQL expression — `content IS NOT NULL` is added to the SELECT, the blob bytes
# are NEVER transferred (content stays deferred). Lets the API/WebUI tell which
# files are editable in place without dragging the body along.
Node.inline = column_property(Node.content.isnot(None))


class Part(Base):
    __tablename__ = "parts"

    file_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    # per-part channel: an interrupted cross-channel move leaves a readable,
    # resumable state (each part knows where its message actually lives)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # raw Telegram document.id (legacy `fileid`), account-independent integrity
    # check; NULL = unknown (legacy records with garbage fileid)
    doc_id: Mapped[int | None] = mapped_column(BigInteger)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # dedupe for the bot listener / channel watcher imports
        Index("ix_parts_channel_message", "channel_id", "message_id"),
    )


class TgSession(Base):
    __tablename__ = "tg_sessions"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    api_id: Mapped[int] = mapped_column(Integer, nullable=False)
    api_hash: Mapped[str] = mapped_column(Text, nullable=False)
    bot_token: Mapped[str | None] = mapped_column(Text)
    # Telethon StringSession; NULL when session_storage=file (sessions live in
    # {data}/{name}.session, one set per server instance)
    session_string: Mapped[str | None] = mapped_column(Text)
    dc_id: Mapped[int | None] = mapped_column(Integer)
    is_premium: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (CheckConstraint("kind IN ('user', 'bot')", name="ck_tg_sessions_kind"),)


class UploadState(Base):
    __tablename__ = "upload_states"

    file_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    portion_idx: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tg_file_id: Mapped[int | None] = mapped_column(BigInteger)
    parts_saved: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    bytes_done: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class Change(Base):
    __tablename__ = "changes"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    op: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "op IN ('CREATE', 'UPDATE', 'MOVE', 'DELETE')", name="ck_changes_op"
        ),
    )


class OperationJob(Base):
    """A durable, best-effort Web UI bulk operation."""

    __tablename__ = "operation_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    # Snapshot the target id without an FK so job history survives a later purge.
    parent_id: Mapped[str | None] = mapped_column(Text)
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    succeeded: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint("operation IN ('move', 'delete')", name="ck_operation_jobs_operation"),
        CheckConstraint(
            "state IN ('queued', 'running', 'completed', 'failed', 'interrupted')",
            name="ck_operation_jobs_state",
        ),
        CheckConstraint(
            "total >= 0 AND succeeded >= 0 AND failed >= 0 AND skipped >= 0",
            name="ck_operation_jobs_counts_nonnegative",
        ),
        Index("ix_operation_jobs_created_at", "created_at"),
    )


class OperationJobItem(Base):
    """One selected node and its durable result within an operation job."""

    __tablename__ = "operation_job_items"

    job_id: Mapped[str] = mapped_column(
        ForeignKey("operation_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    # No node FK: job history must remain readable after a node is purged.
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
            name="ck_operation_job_items_state",
        ),
        Index("ix_operation_job_items_job_position", "job_id", "position"),
    )
