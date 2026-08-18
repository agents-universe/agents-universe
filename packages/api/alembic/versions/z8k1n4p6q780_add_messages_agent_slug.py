"""add messages.agent_slug

Revision ID: z8k1n4p6q780
Revises: y7j0m8i1j679
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa

revision = 'z8k1n4p6q780'
down_revision = 'y7j0m8i1j679'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("agent_slug", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "agent_slug")
