"""align schema with model declarations: tier width + uq_user_token_service

Revision ID: b5d8e2f7a9c3
Revises: d2e8f4a6c135
Create Date: 2026-09-24

Two model/migration drifts with runtime impact:

- user_tier_models.tier was created as String(10) while the model declares
  String(50); PUT /api/tier-models/{azure_openai|google_gemini} writes 12-13
  char tier names, overflowing VARCHAR(10) on MSSQL/PG/MySQL strict modes.
- user_tokens never got the uq_user_token_service constraint the model
  declares — routers/tokens.upsert_token relies on its IntegrityError to
  settle concurrent creates, and without it duplicate (user_id, service_key)
  rows make every scalar_one_or_none() raise MultipleResultsFound.
"""
from alembic import context, op
import sqlalchemy as sa


revision = 'b5d8e2f7a9c3'
down_revision = 'd2e8f4a6c135'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table: SQLite has no ALTER COLUMN TYPE, so the rewrite
    # path is required there; other dialects get plain ALTERs.
    with op.batch_alter_table("user_tier_models") as batch_op:
        batch_op.alter_column(
            "tier",
            existing_type=sa.String(10),
            type_=sa.String(50),
            existing_nullable=False,
        )

    # Duplicate rows can only exist on a live DB the race actually hit —
    # skipped in offline (--sql) mode like other data fixes.
    if not context.is_offline_mode():
        # Keep the newest row per pair (created_at, then token_id as the
        # tie-breaker) so constraint creation cannot fail on a dirty DB.
        # The derived-table wrap keeps the DELETE valid on MySQL, which
        # forbids selecting from the table being deleted.
        op.execute(sa.text(
            "DELETE FROM user_tokens WHERE token_id NOT IN ("
            "  SELECT keep_id FROM ("
            "    SELECT token_id AS keep_id,"
            "           ROW_NUMBER() OVER ("
            "             PARTITION BY user_id, service_key"
            "             ORDER BY created_at DESC, token_id DESC"
            "           ) AS rn"
            "    FROM user_tokens"
            "  ) ranked WHERE ranked.rn = 1"
            ")"
        ))

    with op.batch_alter_table("user_tokens") as batch_op:
        batch_op.create_unique_constraint(
            "uq_user_token_service", ["user_id", "service_key"]
        )


def downgrade() -> None:
    with op.batch_alter_table("user_tokens") as batch_op:
        batch_op.drop_constraint("uq_user_token_service", type_="unique")

    with op.batch_alter_table("user_tier_models") as batch_op:
        batch_op.alter_column(
            "tier",
            existing_type=sa.String(50),
            type_=sa.String(10),
            existing_nullable=False,
        )
