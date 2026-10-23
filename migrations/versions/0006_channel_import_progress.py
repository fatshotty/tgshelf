"""Store completed channel-history import boundaries.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'channel_import_progress',
        sa.Column('folder_id', sa.Text(), sa.ForeignKey('nodes.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('channel_id', sa.BigInteger(), primary_key=True),
        sa.Column('last_scanned_message_id', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('last_scanned_message_id >= 0', name='ck_channel_import_progress_nonnegative'),
    )


def downgrade() -> None:
    op.drop_table('channel_import_progress')
