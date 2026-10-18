"""Add (project_id, user_id) index on conversations.

Revision ID: b4d7e2a9c518
Revises: c7e4a9f2b6d1
Create Date: 2026-10-18

The web UI polls the conversation list every 5s and every project-scoped
page lists WHERE project_id=? AND user_id=? AND status='active' — without
this index those are full table scans on the three-way join's driving
table (SQL Server does not index FKs automatically).
"""
from alembic import op

revision = "b4d7e2a9c518"
down_revision = "c7e4a9f2b6d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_conversations_project_user",
        "conversations",
        ["project_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_project_user", table_name="conversations")
