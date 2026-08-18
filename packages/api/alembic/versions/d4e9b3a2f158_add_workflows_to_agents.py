"""add workflows to agents

Revision ID: d4e9b3a2f158
Revises: c3f8a2b1d047
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4e9b3a2f158'
down_revision = 'c3f8a2b1d047'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'agents',
        sa.Column('workflows', sa.String(2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('agents', 'workflows')
