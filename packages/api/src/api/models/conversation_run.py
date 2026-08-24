"""Durable record of an agent turn (conversation run).

One row per turn. Created when the user message is persisted; transitions
running → completed | failed | interrupted. `streaming_snapshot` holds a
throttled copy of the partial output for interruption recovery — the final
Message row stays authoritative for completed turns.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class ConversationRun(Base):
    __tablename__ = "conversation_runs"

    __table_args__ = (
        # Latest-run lookup per conversation (REST endpoint + list subquery).
        Index("ix_conversation_runs_conversation_started", "conversation_id", "started_at"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id"), nullable=False
    )
    # No FK to messages.message_id: compression hard-deletes old message rows
    # and a constraint would block it. Plain String(36).
    user_message_id: Mapped[str | None] = mapped_column(String(36))
    # running | completed | failed | interrupted
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_message: Mapped[str | None] = mapped_column(UnicodeText)
    streaming_snapshot: Mapped[str | None] = mapped_column(UnicodeText)
    tokens_used: Mapped[int | None] = mapped_column(Integer)

    conversation: Mapped["Conversation"] = relationship(back_populates="runs")
