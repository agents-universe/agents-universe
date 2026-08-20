"""User credential and model configuration models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Integer, String, Unicode, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class UserToken(Base):
    """Integration service tokens (git, jira, confluence, kong, alice, etc.).
    LLM provider keys have been moved to UserApiKey."""
    __tablename__ = "user_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "service_key", name="uq_user_token_service"),
    )

    token_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    service_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str | None] = mapped_column(Unicode(255))
    encrypted_value: Mapped[str] = mapped_column(String(4000), nullable=False)
    key_hint: Mapped[str | None] = mapped_column(String(10))
    base_url: Mapped[str | None] = mapped_column(String(500))
    model_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class UserApiKey(Base):
    """Encrypted LLM provider API keys — one row per provider per user."""
    __tablename__ = "user_api_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_api_key_provider"),
    )

    key_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(String(4000), nullable=False)
    key_hint: Mapped[str | None] = mapped_column(String(10))
    base_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class UserPreference(Base):
    """Per-user onboarding tour / what's-new read state — one row per user."""
    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_seen_version: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class UserTierModel(Base):
    """User-configured provider→model mapping — one row per provider per user.
    The `tier` column now stores the provider key (e.g. 'anthropic', 'openai').
    DEPRECATED: replaced by UserModelConfig. Kept for backward compat."""
    __tablename__ = "user_tier_models"
    __table_args__ = (
        UniqueConstraint("user_id", "tier", name="uq_user_tier_model"),
    )

    config_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tier: Mapped[str] = mapped_column(String(50), nullable=False)   # now holds provider name
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class UserModelConfig(Base):
    """User model configurations — supports multiple entries per provider."""
    __tablename__ = "user_model_configs"

    config_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    encrypted_key: Mapped[str | None] = mapped_column(String(4000))
    key_hint: Mapped[str | None] = mapped_column(String(10))
    base_url: Mapped[str | None] = mapped_column(String(500))
    url_mode: Mapped[str] = mapped_column(String(20), default="base_url", server_default="base_url")
    # Auto-route tier: "low" | "mid" | "high" | None (None = user hasn't
    # assigned; the model then only serves explicit selection, not auto).
    complexity_tier: Mapped[str | None] = mapped_column(String(20))
    # Context-window override (tokens); None = name-matched default at runtime.
    context_window: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
