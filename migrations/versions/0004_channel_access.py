"""Persist independent account/channel permission overrides."""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_access",
        sa.Column("account_name", sa.Text(), primary_key=True),
        sa.Column("channel_id", sa.BigInteger(), primary_key=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("mode IN ('none', 'read', 'write')", name="ck_channel_access_mode"),
    )
    op.create_index("ix_channel_access_channel_id", "channel_access", ["channel_id"])


def downgrade() -> None:
    op.drop_table("channel_access")
