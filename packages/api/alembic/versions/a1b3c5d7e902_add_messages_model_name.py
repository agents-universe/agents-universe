"""add messages.model_name

Revision ID: a1b3c5d7e902
Revises: d9a5c2e7b123
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b3c5d7e902'
down_revision = 'd9a5c2e7b123'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("model_name", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "model_name")
