"""add agent publishes and publish keys

Revision ID: a3b7d1e2f415
Revises: b3d5f7h9k135
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

revision = 'a3b7d1e2f415'
down_revision = 'b3d5f7h9k135'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- conversations.source (origin marker; NULL = ordinary) -------------
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.add_column(sa.Column("source", sa.String(20), nullable=True))
    else:
        op.add_column("conversations", sa.Column("source", sa.String(20), nullable=True))

    # --- agent_publishes ---------------------------------------------------
    op.create_table(
        "agent_publishes",
        sa.Column("publish_id", sa.String(36), nullable=False),
        sa.Column("owner_id", sa.String(100), nullable=False),
        sa.Column("agent_slug", sa.String(100), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("model_config_id", sa.String(36), nullable=False),
        sa.Column("title", sa.Unicode(255), nullable=True),
        sa.Column("description", sa.UnicodeText(), nullable=True),
        sa.Column("page_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("api_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("publish_id"),
        sa.UniqueConstraint("owner_id", "agent_slug", name="uq_agent_publish_owner_slug"),
    )
    op.create_index("ix_agent_publishes_owner_id", "agent_publishes", ["owner_id"])
    op.create_index("ix_agent_publishes_project_id", "agent_publishes", ["project_id"])

    # --- publish_keys ------------------------------------------------------
    op.create_table(
        "publish_keys",
        sa.Column("key_id", sa.String(36), nullable=False),
        sa.Column("publish_id", sa.String(36), sa.ForeignKey("agent_publishes.publish_id"), nullable=False),
        sa.Column("name", sa.Unicode(100), nullable=True),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_hint", sa.String(10), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("key_id"),
    )
    op.create_index("ix_publish_keys_publish_id", "publish_keys", ["publish_id"])


def downgrade() -> None:
    op.drop_index("ix_publish_keys_publish_id", table_name="publish_keys")
    op.drop_table("publish_keys")
    op.drop_index("ix_agent_publishes_project_id", table_name="agent_publishes")
    op.drop_index("ix_agent_publishes_owner_id", table_name="agent_publishes")
    op.drop_table("agent_publishes")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.drop_column("source")
    else:
        op.drop_column("conversations", "source")
