"""Enable durable copy jobs and account/access diagnostics."""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_operation_jobs_operation", "operation_jobs", type_="check")
    op.create_check_constraint("ck_operation_jobs_operation", "operation_jobs", "operation IN ('move', 'copy', 'delete')")
    op.add_column("operation_job_items", sa.Column("account_name", sa.Text()))
    op.add_column("operation_job_items", sa.Column("error_code", sa.Text()))
    op.add_column("operation_job_items", sa.Column("error_channel_id", sa.BigInteger()))


def downgrade() -> None:
    # Refuse instead of destroying or relabeling operator history.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM operation_jobs WHERE operation = 'copy') THEN
            RAISE EXCEPTION 'Cannot downgrade while copy job history exists';
        END IF;
    END $$""")
    op.drop_column("operation_job_items", "error_channel_id")
    op.drop_column("operation_job_items", "error_code")
    op.drop_column("operation_job_items", "account_name")
    op.drop_constraint("ck_operation_jobs_operation", "operation_jobs", type_="check")
    op.create_check_constraint("ck_operation_jobs_operation", "operation_jobs", "operation IN ('move', 'delete')")
