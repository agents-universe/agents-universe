"""add project visibility and members

Revision ID: x6i9l7h0i568
Revises: w5h8k6g9h457
Create Date: 2026-08-14
"""
from alembic import op
import sqlalchemy as sa

revision = 'x6i9l7h0i568'
down_revision = 'w5h8k6g9h457'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default 同时完成存量行回填(public),对 SQL Server / SQLite 均生效
    op.add_column(
        'projects',
        sa.Column('visibility', sa.String(20), nullable=False, server_default='public'),
    )
    op.create_table(
        'project_members',
        sa.Column('project_id', sa.String(36),
                  sa.ForeignKey('projects.project_id'), primary_key=True, nullable=False),
        sa.Column('user_id', sa.String(100), primary_key=True, nullable=False),
        sa.Column('added_by', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_project_members_user_id', 'project_members', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_project_members_user_id', table_name='project_members')
    op.drop_table('project_members')
    op.drop_column('projects', 'visibility')
