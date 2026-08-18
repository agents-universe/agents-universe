"""add mcp_servers

Revision ID: y7j0m8i1j679
Revises: x6i9l7h0i568
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa

revision = 'y7j0m8i1j679'
down_revision = 'x6i9l7h0i568'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("server_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.project_id"), nullable=True),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.Unicode(200), nullable=False),
        sa.Column("description", sa.Unicode(500), nullable=True),
        sa.Column("transport", sa.String(32), nullable=False, server_default="auto"),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("headers", sa.String(4000), nullable=True),
        sa.Column("auth_type", sa.String(16), nullable=False, server_default="none"),
        sa.Column("secret_ref", sa.String(100), nullable=True),
        sa.Column("secret_scope", sa.String(16), nullable=False, server_default="project"),
        sa.Column("auth_header_name", sa.String(100), nullable=True),
        sa.Column("auth_value_template", sa.String(200), nullable=True),
        sa.Column("options", sa.String(4000), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("command", sa.String(500), nullable=True),
        sa.Column("args", sa.String(2000), nullable=True),
        sa.Column("env", sa.String(4000), nullable=True),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_mcp_servers_project_id", "mcp_servers", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_servers_project_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
