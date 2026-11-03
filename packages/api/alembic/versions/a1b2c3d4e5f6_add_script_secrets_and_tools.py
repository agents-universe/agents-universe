"""add secrets_json and tools_json to automation_scripts

Revision ID: a1b2c3d4e5f6
Revises: cc3c67024ddc
Create Date: 2026-11-03

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'cc3c67024ddc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable plain add_column — dialect-safe across all four dialects.
    op.add_column(
        "automation_scripts",
        sa.Column("secrets_json", sa.Unicode(2000), nullable=True),
    )
    op.add_column(
        "automation_scripts",
        sa.Column("tools_json", sa.Unicode(2000), nullable=True),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("automation_scripts") as batch_op:
            batch_op.drop_column("tools_json")
            batch_op.drop_column("secrets_json")
    else:
        op.drop_column("automation_scripts", "tools_json")
        op.drop_column("automation_scripts", "secrets_json")
