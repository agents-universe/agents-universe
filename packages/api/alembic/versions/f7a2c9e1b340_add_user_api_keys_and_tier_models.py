"""add user_api_keys and user_tier_models

Revision ID: f7a2c9e1b340
Revises: e5f1c4d8b920
Create Date: 2026-06-22
"""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import text

revision = 'f7a2c9e1b340'
down_revision = 'e5f1c4d8b920'
branch_labels = None
depends_on = None

LLM_PROVIDERS = {'anthropic', 'openai', 'azure_openai', 'google_gemini'}


def upgrade() -> None:
    op.create_table(
        'user_api_keys',
        sa.Column('key_id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(100), nullable=False),
        sa.Column('provider', sa.String(50), nullable=False),
        sa.Column('encrypted_value', sa.String(4000), nullable=False),
        sa.Column('key_hint', sa.String(10), nullable=True),
        sa.Column('base_url', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=True),
        sa.UniqueConstraint('user_id', 'provider', name='uq_user_api_key_provider'),
    )

    op.create_table(
        'user_tier_models',
        sa.Column('config_id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(100), nullable=False),
        sa.Column('tier', sa.String(10), nullable=False),
        sa.Column('provider', sa.String(50), nullable=False),
        sa.Column('model_id', sa.String(200), nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=True),
        sa.UniqueConstraint('user_id', 'tier', name='uq_user_tier_model'),
    )

    # Data backfill needs a live DB — skipped in offline (--sql) mode.
    if context.is_offline_mode():
        return

    conn = op.get_bind()

    # Migrate data from user_tokens where provider is an LLM provider
    rows = conn.execute(text(
        "SELECT user_id, service_key, encrypted_value, key_hint, base_url, model_id, created_at "
        "FROM user_tokens ORDER BY created_at ASC"
    )).fetchall()

    seen_api_keys: set[tuple] = set()   # (user_id, provider)
    seen_tier_models: set[tuple] = set()  # (user_id, tier)

    import uuid
    from datetime import datetime

    for row in rows:
        user_id, service_key, encrypted_value, key_hint, base_url, model_id, created_at = row
        parts = service_key.split(':', 1)
        provider = parts[0]
        tier = parts[1] if len(parts) > 1 else None

        if provider not in LLM_PROVIDERS:
            continue  # integration token — leave in user_tokens

        # Upsert into user_api_keys (last-write-wins since rows are ordered by created_at ASC)
        key = (user_id, provider)
        if key not in seen_api_keys:
            conn.execute(text(
                "INSERT INTO user_api_keys "
                "(key_id, user_id, provider, encrypted_value, key_hint, base_url, created_at) "
                "VALUES (:kid, :uid, :prov, :enc, :hint, :url, :cat)"
            ), {
                'kid': str(uuid.uuid4()),
                'uid': user_id,
                'prov': provider,
                'enc': encrypted_value,
                'hint': key_hint,
                'url': base_url,
                'cat': created_at or datetime.utcnow(),
            })
            seen_api_keys.add(key)
        else:
            # Update with newer values
            conn.execute(text(
                "UPDATE user_api_keys SET encrypted_value=:enc, key_hint=:hint, base_url=:url "
                "WHERE user_id=:uid AND provider=:prov"
            ), {'enc': encrypted_value, 'hint': key_hint, 'url': base_url, 'uid': user_id, 'prov': provider})

        # Migrate model_id into user_tier_models
        if tier and model_id:
            tk = (user_id, tier)
            if tk not in seen_tier_models:
                conn.execute(text(
                    "INSERT INTO user_tier_models "
                    "(config_id, user_id, tier, provider, model_id, created_at) "
                    "VALUES (:cid, :uid, :tier, :prov, :mid, :cat)"
                ), {
                    'cid': str(uuid.uuid4()),
                    'uid': user_id,
                    'tier': tier,
                    'prov': provider,
                    'mid': model_id,
                    'cat': created_at or datetime.utcnow(),
                })
                seen_tier_models.add(tk)


def downgrade() -> None:
    op.drop_table('user_tier_models')
    op.drop_table('user_api_keys')
