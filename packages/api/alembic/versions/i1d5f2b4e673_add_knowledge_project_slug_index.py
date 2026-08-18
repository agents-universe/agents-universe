"""Add (project_id, slug) index to knowledge_metadata.

Revision ID: i1d5f2b4e673
Revises: f7b3d2a1e456
Create Date: 2026-07-07
"""
from alembic import op

revision = "i1d5f2b4e673"
down_revision = "f7b3d2a1e456"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_knowledge_project_slug",
        "knowledge_metadata",
        ["project_id", "slug"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_project_slug", table_name="knowledge_metadata")
