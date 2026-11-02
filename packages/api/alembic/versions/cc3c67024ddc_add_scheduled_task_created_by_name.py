"""add created_by_name to scheduled_tasks

Revision ID: cc3c67024ddc
Revises: d8e4f2a6b9c7
Create Date: 2026-11-03

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'cc3c67024ddc'
down_revision = 'd8e4f2a6b9c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable plain add_column — dialect-safe across all four dialects.
    op.add_column(
        "scheduled_tasks",
        sa.Column("created_by_name", sa.Unicode(255), nullable=True),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("scheduled_tasks") as batch_op:
            batch_op.drop_column("created_by_name")
    else:
        op.drop_column("scheduled_tasks", "created_by_name")
