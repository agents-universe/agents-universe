"""Add durable project deletion jobs.

Revision ID: p8r2s4t6u890
Revises: o7k1m2n3p456
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mssql

revision = "p8r2s4t6u890"
down_revision = "o7k1m2n3p456"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    uuid_type = mssql.UNIQUEIDENTIFIER() if bind.dialect.name == "mssql" else sa.String(36)
    uuid_default = sa.text("NEWID()") if bind.dialect.name == "mssql" else None
    op.create_table(
        "project_deletion_jobs",
        sa.Column("job_id", uuid_type, nullable=False, server_default=uuid_default),
        sa.Column("project_id", uuid_type, nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("owner_id", sa.String(100), nullable=True),
        sa.Column("source_path", sa.Unicode(1000), nullable=False),
        sa.Column("trash_path", sa.Unicode(1000), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="prepared"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Unicode(2000), nullable=True),
        sa.Column("prepared_at", sa.DateTime(), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(), nullable=True),
        sa.Column("db_deleted_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("ix_project_deletion_jobs_project_id", "project_deletion_jobs", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_deletion_jobs_project_id", table_name="project_deletion_jobs")
    op.drop_table("project_deletion_jobs")
