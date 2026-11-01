"""add email_servers

Revision ID: d8e4f2a6b9c7
Revises: f4a8b2c9d517
Create Date: 2026-11-01

User-scoped IMAP/SMTP integration rows (Settings → Integrations → 邮件服务器).
Password is AES-256-GCM encrypted with token_vault's user_id-derived key;
only the last-4 key_hint is stored in clear. Client-side String(36) PK —
no IDENTITY, portable across all four dialects.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd8e4f2a6b9c7'
down_revision = 'f4a8b2c9d517'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_servers",
        sa.Column("email_server_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("display_name", sa.Unicode(255), nullable=True),
        sa.Column("imap_host", sa.String(255), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("imap_security", sa.String(10), nullable=False, server_default="ssl"),
        sa.Column("smtp_host", sa.String(255), nullable=False),
        sa.Column("smtp_port", sa.Integer(), nullable=False, server_default="587"),
        sa.Column("smtp_security", sa.String(10), nullable=False, server_default="starttls"),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("encrypted_password", sa.String(4000), nullable=False),
        sa.Column("key_hint", sa.String(10), nullable=True),
        sa.Column("allow_private_network", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "slug", name="uq_email_server_slug"),
    )
    op.create_index("ix_email_servers_user_id", "email_servers", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_email_servers_user_id", table_name="email_servers")
    op.drop_table("email_servers")
