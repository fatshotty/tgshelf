"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-12

Full initial schema as per docs/PLAN.md: nodes/parts/tg_sessions/upload_states
and the append-only `changes` feed with its triggers + pg_notify. The triggers
are the only code living in the DB (user decision): they guarantee the feed is
atomic and complete across multiple tool instances and out-of-band writes.
Path resolution lives in Python (NodeRepo recursive CTE query), not in a DB
function.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ROOT_ID = "0000000000"


def upgrade() -> None:
    op.create_table(
        "nodes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("parent_id", sa.Text(), sa.ForeignKey("nodes.id"), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_folder", sa.Boolean(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("info", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "ctime", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "mtime", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("state IN ('ACTIVE', 'TEMP', 'DELETED')", name="ck_nodes_state"),
        sa.CheckConstraint(
            f"id = '{ROOT_ID}' OR parent_id IS NOT NULL",
            name="ck_nodes_root_only_null_parent",
        ),
    )
    op.create_index("ix_nodes_parent_id", "nodes", ["parent_id"])
    op.create_index(
        "uq_nodes_parent_lower_name_active",
        "nodes",
        ["parent_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
    )
    op.create_index(
        "ix_nodes_lower_name",
        "nodes",
        [sa.text("lower(name) text_pattern_ops")],
    )
    op.create_index(
        "ix_nodes_channel_id",
        "nodes",
        ["channel_id"],
        postgresql_where=sa.text("channel_id IS NOT NULL"),
    )

    op.create_table(
        "parts",
        sa.Column(
            "file_id",
            sa.Text(),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("idx", sa.Integer(), primary_key=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("doc_id", sa.BigInteger(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=True),
    )
    op.create_index("ix_parts_channel_message", "parts", ["channel_id", "message_id"])

    op.create_table(
        "tg_sessions",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("api_id", sa.Integer(), nullable=False),
        sa.Column("api_hash", sa.Text(), nullable=False),
        sa.Column("bot_token", sa.Text(), nullable=True),
        sa.Column("session_string", sa.Text(), nullable=True),
        sa.Column("dc_id", sa.Integer(), nullable=True),
        sa.Column("is_premium", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.CheckConstraint("kind IN ('user', 'bot')", name="ck_tg_sessions_kind"),
    )

    op.create_table(
        "upload_states",
        sa.Column(
            "file_id",
            sa.Text(),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("portion_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tg_file_id", sa.BigInteger(), nullable=True),
        sa.Column("parts_saved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_done", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "changes",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column(
            "at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("op IN ('CREATE', 'UPDATE', 'MOVE', 'DELETE')", name="ck_changes_op"),
    )

    # -- changes feed: trigger functions + triggers (toggleable at startup via
    # ALTER TABLE ... ENABLE/DISABLE TRIGGER when changes_feed.enabled is off)
    op.execute(
        """
        CREATE FUNCTION nodes_changes_fn() RETURNS trigger AS $$
        DECLARE
          v_op TEXT;
          v_node_id TEXT;
          v_seq BIGINT;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            v_op := 'CREATE';
            v_node_id := NEW.id;
          ELSIF TG_OP = 'UPDATE' THEN
            IF NEW.parent_id IS DISTINCT FROM OLD.parent_id THEN
              v_op := 'MOVE';
            ELSE
              v_op := 'UPDATE';
            END IF;
            v_node_id := NEW.id;
          ELSE
            v_op := 'DELETE';
            v_node_id := OLD.id;
          END IF;
          INSERT INTO changes (node_id, op) VALUES (v_node_id, v_op) RETURNING seq INTO v_seq;
          PERFORM pg_notify(
            'changes',
            json_build_object('seq', v_seq, 'node_id', v_node_id, 'op', v_op)::text
          );
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION parts_changes_fn() RETURNS trigger AS $$
        DECLARE
          v_node_id TEXT;
          v_seq BIGINT;
        BEGIN
          v_node_id := COALESCE(NEW.file_id, OLD.file_id);
          -- cascade delete of the file already emitted its DELETE event
          IF NOT EXISTS (SELECT 1 FROM nodes WHERE id = v_node_id) THEN
            RETURN NULL;
          END IF;
          INSERT INTO changes (node_id, op) VALUES (v_node_id, 'UPDATE') RETURNING seq INTO v_seq;
          PERFORM pg_notify(
            'changes',
            json_build_object('seq', v_seq, 'node_id', v_node_id, 'op', 'UPDATE')::text
          );
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_nodes_changes
        AFTER INSERT OR UPDATE OR DELETE ON nodes
        FOR EACH ROW EXECUTE FUNCTION nodes_changes_fn()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_parts_changes
        AFTER INSERT OR UPDATE OR DELETE ON parts
        FOR EACH ROW EXECUTE FUNCTION parts_changes_fn()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_parts_changes ON parts")
    op.execute("DROP TRIGGER trg_nodes_changes ON nodes")
    op.execute("DROP FUNCTION parts_changes_fn()")
    op.execute("DROP FUNCTION nodes_changes_fn()")
    op.drop_table("changes")
    op.drop_table("upload_states")
    op.drop_table("tg_sessions")
    op.drop_table("parts")
    op.drop_table("nodes")
