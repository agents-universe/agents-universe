"""add script run result and spec slug

Revision ID: t6f9a3b5d820
Revises: f4b8c2e6a915
Create Date: 2026-09-15

Structured Playwright results (counts, failed cases, artifacts) plus the spec a
run belongs to. Both columns are nullable with no backfill: pre-existing rows
keep NULL and the UI falls back to the raw log.
"""
import sqlalchemy as sa
from alembic import context, op

revision = "t6f9a3b5d820"
down_revision = "f4b8c2e6a915"
branch_labels = None
depends_on = None

_INDEX = "ix_script_runs_spec_created"
_COLUMNS = [
    ("spec_slug", sa.String(100)),
    ("result_json", sa.UnicodeText()),
]


def upgrade() -> None:
    # Offline (--sql) mode has no connection to reflect - treat it as the
    # fresh-DB case and emit every statement.
    if context.is_offline_mode():
        for name, col_type in _COLUMNS:
            op.add_column("script_runs", sa.Column(name, col_type, nullable=True))
        op.create_index(_INDEX, "script_runs", ["script_id", "spec_slug", "created_at"])
        return

    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("script_runs")}
    for name, col_type in _COLUMNS:
        if name not in existing:
            op.add_column("script_runs", sa.Column(name, col_type, nullable=True))

    # Per-spec history filters on exactly this triple and orders by created_at.
    if not any(ix["name"] == _INDEX for ix in insp.get_indexes("script_runs")):
        op.create_index(_INDEX, "script_runs", ["script_id", "spec_slug", "created_at"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="script_runs")
    op.drop_column("script_runs", "result_json")
    op.drop_column("script_runs", "spec_slug")
