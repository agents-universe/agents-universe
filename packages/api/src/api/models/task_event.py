"""Durable task execution timeline events."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, UnicodeText, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_task_event_conversation_sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    payload: Mapped[str] = mapped_column(UnicodeText, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc, nullable=False)

    conversation: Mapped["Conversation"] = relationship(back_populates="task_events")
