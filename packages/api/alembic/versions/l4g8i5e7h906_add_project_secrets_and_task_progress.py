"""Add project_secrets table and task progress columns.

Revision ID: l4g8i5e7h906
Revises: k3f7h4d6g895
Create Date: 2026-07-17
"""
import sqlalchemy as sa
from alembic import context, op

revision = "l4g8i5e7h906"
down_revision = "k3f7h4d6g895"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Offline (--sql) mode has no live connection to reflect — treat as the
    # fresh-DB case (everything missing).
    if context.is_offline_mode():
        op.create_table(
            "project_secrets",
            sa.Column("secret_id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("service_key", sa.String(100), nullable=False),
            sa.Column("environment", sa.String(100), nullable=True),
            sa.Column("secret_name", sa.String(100), nullable=False, server_default="default"),
            sa.Column("display_name", sa.Unicode(255), nullable=True),
            sa.Column("encrypted_value", sa.String(4000), nullable=False),
            sa.Column("key_hint", sa.String(10), nullable=True),
            sa.Column("created_by", sa.String(100), nullable=False),
            sa.Column("updated_by", sa.String(100), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("project_id", "service_key", "environment", "secret_name", name="uq_project_secret_key"),
        )
        op.create_index("ix_project_secrets_project", "project_secrets", ["project_id", "is_active"])
        for col_name, col_type in [
            ("current_step", sa.Unicode(500)),
            ("next_step", sa.Unicode(500)),
            ("progress_completed", sa.Integer()),
            ("progress_total", sa.Integer()),
        ]:
            op.add_column("agent_tasks", sa.Column(col_name, col_type, nullable=True))
        return

    # Reflection instead of INFORMATION_SCHEMA — the latter has no SQLite
    # equivalent, so the old queries crashed there.
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Create project_secrets table if not exists
    if not insp.has_table("project_secrets"):
        op.create_table(
            "project_secrets",
            sa.Column("secret_id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("service_key", sa.String(100), nullable=False),
            sa.Column("environment", sa.String(100), nullable=True),
            sa.Column("secret_name", sa.String(100), nullable=False, server_default="default"),
            sa.Column("display_name", sa.Unicode(255), nullable=True),
            sa.Column("encrypted_value", sa.String(4000), nullable=False),
            sa.Column("key_hint", sa.String(10), nullable=True),
            sa.Column("created_by", sa.String(100), nullable=False),
            sa.Column("updated_by", sa.String(100), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("project_id", "service_key", "environment", "secret_name", name="uq_project_secret_key"),
        )
        op.create_index("ix_project_secrets_project", "project_secrets", ["project_id", "is_active"])

    # AgentTask progress columns (idempotent)
    task_cols = [c["name"] for c in insp.get_columns("agent_tasks")]
    for col_name, col_type in [
        ("current_step", sa.Unicode(500)),
        ("next_step", sa.Unicode(500)),
        ("progress_completed", sa.Integer()),
        ("progress_total", sa.Integer()),
    ]:
        if col_name not in task_cols:
            op.add_column("agent_tasks", sa.Column(col_name, col_type, nullable=True))


def downgrade() -> None:
    op.drop_column("agent_tasks", "progress_total")
    op.drop_column("agent_tasks", "progress_completed")
    op.drop_column("agent_tasks", "next_step")
    op.drop_column("agent_tasks", "current_step")
    op.drop_index("ix_project_secrets_project", table_name="project_secrets")
    op.drop_table("project_secrets")
