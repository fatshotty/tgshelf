"""add durable operation jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03

Bulk Web UI actions outlive an HTTP request and must retain their per-node
results. Node and target ids deliberately have no foreign keys so a future hard
purge does not erase this operational history.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("operation IN ('move', 'delete')", name="ck_operation_jobs_operation"),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'completed', 'failed', 'interrupted')",
            name="ck_operation_jobs_state",
        ),
        sa.CheckConstraint(
            "total >= 0 AND succeeded >= 0 AND failed >= 0 AND skipped >= 0",
            name="ck_operation_jobs_counts_nonnegative",
        ),
    )
    op.create_index("ix_operation_jobs_created_at", "operation_jobs", ["created_at"])

    op.create_table(
        "operation_job_items",
        sa.Column(
            "job_id",
            sa.Text(),
            sa.ForeignKey("operation_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=True),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
            name="ck_operation_job_items_state",
        ),
    )
    op.create_index(
        "ix_operation_job_items_job_position",
        "operation_job_items",
        ["job_id", "position"],
    )


def downgrade() -> None:
    op.drop_index("ix_operation_job_items_job_position", table_name="operation_job_items")
    op.drop_table("operation_job_items")
    op.drop_index("ix_operation_jobs_created_at", table_name="operation_jobs")
    op.drop_table("operation_jobs")
