"""add project_id to agents

Revision ID: r1b4c6d8e023
Revises: q9a3b5c7d012
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = 'r1b4c6d8e023'
down_revision = 'q9a3b5c7d012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite has no ALTER TABLE ADD COLUMN with a FOREIGN KEY — batch
        # mode rebuilds the table instead (batch requires named constraints).
        with op.batch_alter_table("agents") as batch_op:
            batch_op.add_column(sa.Column("project_id", sa.String(36), nullable=True))
            batch_op.create_foreign_key("fk_agents_project_id", "projects", ["project_id"], ["project_id"])
    else:
        op.add_column(
            "agents",
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.project_id"), nullable=True),
        )
    op.create_index("ix_agents_project_id", "agents", ["project_id"])


def downgrade() -> None:
    # MySQL refuses to drop the last index serving an FK (1553) or an
    # FK-referenced column (1828) — resolve the generated constraint name
    # first. PG auto-drops the column's own FK; SQLite's batch rebuild
    # carries no constraints.
    if op.get_bind().dialect.name == "mysql" and not op.get_context().as_sql:
        for fk in sa.inspect(op.get_bind()).get_foreign_keys("agents"):
            if fk.get("constrained_columns") == ["project_id"]:
                op.drop_constraint(fk["name"], "agents", type_="foreignkey")
    op.drop_index("ix_agents_project_id", table_name="agents")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("agents") as batch_op:
            batch_op.drop_column("project_id")
    else:
        op.drop_column("agents", "project_id")
