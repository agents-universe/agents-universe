"""add agent_publishes response_mode

Revision ID: ff963c767f62
Revises: a1b2c3d4e5f6
Create Date: 2026-11-21

Per-publish response shape for the external API: "stream" (SSE, the
pre-existing behavior) or "json" (one JSON body collected to completion).
Constant string default, so the ADD COLUMN needs no table rewrite on any
dialect; legacy rows backfill to "stream" and keep streaming.
"""
import sqlalchemy as sa
from alembic import context, op

revision = "ff963c767f62"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Offline (--sql) mode has no connection to reflect — emit directly.
    if context.is_offline_mode():
        op.add_column(
            "agent_publishes",
            sa.Column(
                "response_mode",
                sa.String(16),
                nullable=False,
                server_default="stream",
            ),
        )
        return
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("agent_publishes")}
    if "response_mode" not in existing:
        op.add_column(
            "agent_publishes",
            sa.Column(
                "response_mode",
                sa.String(16),
                nullable=False,
                server_default="stream",
            ),
        )


def downgrade() -> None:
    op.drop_column("agent_publishes", "response_mode")
