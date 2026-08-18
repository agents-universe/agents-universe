"""Conversation, message, and agent task models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, Unicode, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class Conversation(Base):
    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id"))
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str | None] = mapped_column(Unicode(255))
    status: Mapped[str] = mapped_column(String(50), default="active")
    token_budget: Mapped[int] = mapped_column(Integer, default=128000)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", order_by="Message.sequence_num", cascade="all, delete-orphan")
    tasks: Mapped[list["AgentTask"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")
    task_events: Mapped[list["TaskEvent"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    # every turn queries history/Max(sequence_num) scoped to one
    # conversation — without this index those are full table scans on the
    # largest table in the DB (SQL Server doesn't index FKs automatically).
    __table_args__ = (
        Index("ix_messages_conversation_sequence", "conversation_id", "sequence_num"),
    )

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(UnicodeText, nullable=False)
    # Agent that produced (assistant) or was addressed by (user) this
    # message. @-mention turns run a different agent than the conversation
    # default - without per-message attribution the UI cannot tell who said
    # what after a reload. Slug, not agents.agent_id FK: the turn resolves
    # agents by slug from definition files, and project-scoped agents shadow
    # global ones by slug (no cross-dialect FK needed).
    agent_slug: Mapped[str | None] = mapped_column(String(100))
    tool_calls: Mapped[str | None] = mapped_column(UnicodeText)      # JSON
    knowledge_refs: Mapped[str | None] = mapped_column(UnicodeText)  # JSON
    token_count: Mapped[int | None] = mapped_column(Integer)
    sequence_num: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"), nullable=False)
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.task_id"))
    sequence_num: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(Unicode(500), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    tools_needed: Mapped[str | None] = mapped_column(String(500))   # JSON array
    depends_on: Mapped[str | None] = mapped_column(String(500))     # JSON array
    estimated_complexity: Mapped[str | None] = mapped_column(String(20))
    actual_model: Mapped[str | None] = mapped_column(String(100))
    result_summary: Mapped[str | None] = mapped_column(Unicode(2000))
    error_message: Mapped[str | None] = mapped_column(Unicode(2000))
    current_step: Mapped[str | None] = mapped_column(Unicode(500))
    next_step: Mapped[str | None] = mapped_column(Unicode(500))
    progress_completed: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)

    conversation: Mapped["Conversation"] = relationship(back_populates="tasks")
    subtasks: Mapped[list["AgentTask"]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        foreign_keys="[AgentTask.parent_task_id]",
    )
    parent: Mapped["AgentTask | None"] = relationship(
        back_populates="subtasks",
        remote_side=[task_id],
    )
