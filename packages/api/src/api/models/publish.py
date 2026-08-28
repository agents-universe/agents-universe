"""Agent-as-a-Service: published agents and their API keys.

A published agent (``AgentPublish``) exposes an agent plus its project
resources as an SSE API and a SSO chat page. The publisher binds one of
their model configs (``model_config_id``) — every run executes under that
model, whether it comes from the external API or the embedded page.

API keys (``PublishKey``) are scoped per publish. Only the SHA-256 hash is
stored; the plaintext is shown once at issue time and never persisted,
logged, or echoed back.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Unicode, UnicodeText, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class AgentPublish(Base):
    """A published agent: external SSE API + embedded SSO chat page."""

    __tablename__ = "agent_publishes"
    __table_args__ = (
        # Owner + slug are unique together: one publish per (publisher, agent).
        UniqueConstraint("owner_id", "agent_slug", name="uq_agent_publish_owner_slug"),
    )

    publish_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    owner_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # Slug of the published agent (project-scoped or global). Not an FK: the
    # agent may be project-scoped and its definition file is the source of truth.
    agent_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    # Project the agent runs against. Owned by the publisher (their workspace),
    # so a headless external call executes with the publisher's project context.
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id"), nullable=False, index=True
    )
    # Publisher's model config that every run of this publish is pinned to.
    # No FK: user_model_configs rows are user-owned and may be soft-deleted;
    # the pinned config is resolved by config_id at run time.
    model_config_id: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str | None] = mapped_column(Unicode(255))
    description: Mapped[str | None] = mapped_column(UnicodeText)
    # Display metadata for the embedded page (frontend-only).
    page_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # External API enabled/disabled without deleting the publish.
    api_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    keys: Mapped[list["PublishKey"]] = relationship(
        back_populates="publish", cascade="all, delete-orphan"
    )


class PublishKey(Base):
    """API key for a published agent (Bearer / X-API-Key auth).

    Only the SHA-256 hash is stored — the plaintext is shown once at issue
    and never kept. ``key_hint`` holds the last 4 chars so the UI can tell
    keys apart without revealing them.
    """

    __tablename__ = "publish_keys"
    __table_args__ = (
        Index("ix_publish_keys_publish_id", "publish_id"),
    )

    key_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    publish_id: Mapped[str] = mapped_column(
        ForeignKey("agent_publishes.publish_id"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(Unicode(100))
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hint: Mapped[str | None] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    publish: Mapped["AgentPublish"] = relationship(back_populates="keys")
