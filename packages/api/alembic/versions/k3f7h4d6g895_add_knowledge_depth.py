"""Add depth column to knowledge_metadata for hierarchical knowledge.

Revision ID: k3f7h4d6g895
Revises: j2e6g3c5f784
Create Date: 2026-07-17
"""
import sqlalchemy as sa
from alembic import op

revision = "k3f7h4d6g895"
down_revision = "j2e6g3c5f784"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_metadata", sa.Column("depth", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_knowledge_project_depth", "knowledge_metadata", ["project_id", "depth"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_project_depth", table_name="knowledge_metadata")
    op.drop_column("knowledge_metadata", "depth")
