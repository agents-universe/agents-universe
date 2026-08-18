"""drop project fs_path column

Revision ID: h9c4e1a3d562
Revises: g8b3d0f2c451
Create Date: 2026-06-30

fs_path is always computed at runtime as PROJECTS_ROOT / slug — storing it
in the DB caused stale paths when PROJECTS_ROOT changed.
"""
from alembic import op

revision = 'h9c4e1a3d562'
down_revision = 'g8b3d0f2c451'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('projects', 'fs_path')


def downgrade() -> None:
    import sqlalchemy as sa
    op.add_column('projects', sa.Column('fs_path', sa.Unicode(500), nullable=True))
