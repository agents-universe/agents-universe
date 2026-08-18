"""Automation script and run models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, Unicode, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class AutomationScript(Base):
    __tablename__ = "automation_scripts"

    script_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False)
    name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Unicode(2000))
    script_type: Mapped[str] = mapped_column(String(50), default="python")  # workflow|python|bash|playwright
    content: Mapped[str] = mapped_column(UnicodeText, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    runs: Mapped[list["ScriptRun"]] = relationship(back_populates="script")


class ScriptRun(Base):
    __tablename__ = "script_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    script_id: Mapped[str] = mapped_column(ForeignKey("automation_scripts.script_id"), nullable=False)
    triggered_by: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="pending")  # pending|running|completed|failed
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout_log: Mapped[str | None] = mapped_column(UnicodeText)
    stderr_log: Mapped[str | None] = mapped_column(UnicodeText)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)

    script: Mapped["AutomationScript"] = relationship(back_populates="runs")
