"""Personal and episodic memory models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Unicode, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class PersonalMemory(Base):
    __tablename__ = "personal_memories"

    memory_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id"))  # NULL = global
    content: Mapped[str] = mapped_column(Unicode(4000), nullable=False)
    tags: Mapped[str | None] = mapped_column(Unicode(500))       # JSON string array
    created_by: Mapped[str] = mapped_column(String(50), default="user")  # "user" | "agent:{slug}"
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)


class EpisodicMemory(Base):
    __tablename__ = "episodic_memories"
    # One summary per conversation — the background generation task can fire
    # more than once (retries, restart races); the constraint is the final
    # guard against duplicate episodes.
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_episodic_memories_conversation_id"),)

    episode_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False)
    summary: Mapped[str] = mapped_column(Unicode(4000), nullable=False)
    key_findings: Mapped[str | None] = mapped_column(Unicode(2000))   # JSON string array
    open_questions: Mapped[str | None] = mapped_column(Unicode(2000)) # JSON string array
    generated_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
