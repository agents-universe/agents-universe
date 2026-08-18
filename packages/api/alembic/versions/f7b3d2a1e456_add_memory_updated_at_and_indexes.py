"""Add updated_at to personal_memories and performance indexes.

Revision ID: f7b3d2a1e456
Revises: e5f1c4d8b920
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision = "f7b3d2a1e456"
down_revision = "h9c4e1a3d562"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("personal_memories", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_pm_user_project",
        "personal_memories",
        ["user_id", "project_id"],
    )
    op.create_index(
        "ix_em_user_project",
        "episodic_memories",
        ["user_id", "project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_em_user_project", table_name="episodic_memories")
    op.drop_index("ix_pm_user_project", table_name="personal_memories")
    op.drop_column("personal_memories", "updated_at")
