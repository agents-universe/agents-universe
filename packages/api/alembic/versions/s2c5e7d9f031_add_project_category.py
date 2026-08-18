"""add category to projects

Revision ID: s2c5e7d9f031
Revises: r1b4c6d8e023
Create Date: 2026-08-10
"""
from alembic import op
import sqlalchemy as sa

revision = 's2c5e7d9f031'
down_revision = 'r1b4c6d8e023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default 同时完成存量行回填(software),对 SQL Server / SQLite 均生效
    op.add_column(
        'projects',
        sa.Column('category', sa.Unicode(50), nullable=False, server_default='software'),
    )


def downgrade() -> None:
    op.drop_column('projects', 'category')
