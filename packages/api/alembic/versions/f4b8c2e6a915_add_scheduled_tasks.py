"""add scheduled tasks and runs

Revision ID: f4b8c2e6a915
Revises: e2b9d4f6a812
Create Date: 2026-09-09

Cron-driven runs of automation scripts, generated Playwright specs, or headless
agent turns. Both tables are new — no backfill, so no offline-mode guard.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f4b8c2e6a915'
down_revision = 'e2b9d4f6a812'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("schedule_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("name", sa.Unicode(255), nullable=False),
        sa.Column("description", sa.Unicode(2000), nullable=True),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("script_id", sa.String(36), sa.ForeignKey("automation_scripts.script_id"), nullable=True),
        sa.Column("spec_slug", sa.String(100), nullable=True),
        sa.Column("agent_slug", sa.String(100), nullable=True),
        sa.Column("prompt", sa.UnicodeText(), nullable=True),
        sa.Column("env_json", sa.Unicode(2000), nullable=True),
        sa.Column("cron_expr", sa.String(100), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(20), nullable=True),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("notify", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("schedule_id"),
    )
    op.create_index("ix_scheduled_tasks_due", "scheduled_tasks", ["enabled", "next_run_at"])
    op.create_index("ix_scheduled_tasks_project", "scheduled_tasks", ["project_id"])

    op.create_table(
        "scheduled_task_runs",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("schedule_id", sa.String(36), sa.ForeignKey("scheduled_tasks.schedule_id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False, server_default="schedule"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("script_run_id", sa.String(36), nullable=True),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("summary", sa.Unicode(2000), nullable=True),
        sa.Column("error", sa.Unicode(2000), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_scheduled_task_runs_schedule", "scheduled_task_runs", ["schedule_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_task_runs_schedule", table_name="scheduled_task_runs")
    op.drop_table("scheduled_task_runs")
    op.drop_index("ix_scheduled_tasks_project", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_due", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
