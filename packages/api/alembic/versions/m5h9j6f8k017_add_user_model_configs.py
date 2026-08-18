"""add user_model_configs table

Revision ID: m5h9j6f8k017
Revises: l4g8i5e7h906
Create Date: 2026-07-21
"""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import text

revision = 'm5h9j6f8k017'
down_revision = 'l4g8i5e7h906'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NEWID() is T-SQL-only; other dialects rely on the ORM's client-side
    # new_uuid() default (migrations always insert with explicit UUIDs).
    server_default = sa.text("NEWID()") if op.get_bind().dialect.name == "mssql" else None
    op.create_table(
        'user_model_configs',
        sa.Column('config_id', sa.String(36), primary_key=True, server_default=server_default),
        sa.Column('user_id', sa.String(100), nullable=False),
        sa.Column('provider', sa.String(50), nullable=False),
        sa.Column('model_id', sa.String(200), nullable=False),
        sa.Column('encrypted_key', sa.String(4000), nullable=True),
        sa.Column('key_hint', sa.String(10), nullable=True),
        sa.Column('base_url', sa.String(500), nullable=True),
        sa.Column('sort_order', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=True),
    )
    op.create_index('ix_user_model_configs_user', 'user_model_configs', ['user_id'])

    # Migrate existing data: join user_api_keys + user_tier_models.
    # Data backfill needs a live DB — skipped in offline (--sql) mode.
    if context.is_offline_mode():
        return

    conn = op.get_bind()

    import uuid
    from datetime import datetime, timezone

    rows = conn.execute(text(
        "SELECT t.user_id, t.provider, t.model_id, k.encrypted_value, k.key_hint, k.base_url, t.created_at "
        "FROM user_tier_models t "
        "LEFT JOIN user_api_keys k ON t.user_id = k.user_id AND t.provider = k.provider "
        "ORDER BY t.user_id, t.created_at"
    )).fetchall()

    for i, row in enumerate(rows):
        user_id, provider, model_id, enc_value, key_hint, base_url, created_at = row
        conn.execute(text(
            "INSERT INTO user_model_configs "
            "(config_id, user_id, provider, model_id, encrypted_key, key_hint, base_url, sort_order, created_at) "
            "VALUES (:cid, :uid, :prov, :mid, :enc, :hint, :url, :sort, :cat)"
        ), {
            'cid': str(uuid.uuid4()),
            'uid': user_id,
            'prov': provider,
            'mid': model_id,
            'enc': enc_value,
            'hint': key_hint,
            'url': base_url,
            'sort': i,
            'cat': created_at or datetime.now(timezone.utc),
        })


def downgrade() -> None:
    op.drop_index('ix_user_model_configs_user', table_name='user_model_configs')
    op.drop_table('user_model_configs')
