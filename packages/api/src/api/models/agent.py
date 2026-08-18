"""Agent model."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Unicode, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class Agent(Base):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.project_id"), nullable=True, index=True
    )
    display_name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Unicode(2000))
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="agile-development", server_default="agile-development")
    definition_path: Mapped[str | None] = mapped_column(Unicode(500))
    # JSON: {provider, model, deployment?}
    model_low: Mapped[str | None] = mapped_column(String(500))
    model_mid: Mapped[str | None] = mapped_column(String(500))
    model_high: Mapped[str | None] = mapped_column(String(500))
    system_prompt: Mapped[str | None] = mapped_column(UnicodeText)
    skills: Mapped[str | None] = mapped_column(Unicode(2000))  # JSON array
    workflows: Mapped[str | None] = mapped_column(Unicode(2000))  # JSON array
    tools: Mapped[str | None] = mapped_column(String(1000))   # JSON array
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
