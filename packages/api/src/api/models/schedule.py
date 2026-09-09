"""Scheduled task models — time-triggered runs of scripts, specs, or agent turns.

A ``ScheduledTask`` is one cron entry owned by a project. Its target is exactly
one of three kinds, discriminated by ``target_type``:

- ``script``      — an ``automation_scripts`` row (``script_id``);
- ``playwright``  — a generated spec in the project workspace (``spec_slug``);
- ``agent``       — a prompt handed to an agent for a headless turn (``agent_slug`` + ``prompt``).

Each fire writes a ``ScheduledTaskRun``. Script and Playwright runs additionally
reuse the existing ``script_runs`` row (``script_run_id``), so the live-log
WebSocket and the "refuse to delete a project with a running script" guard work
unchanged.

``conversation_id`` is deliberately not a foreign key: it names where the result
summary is delivered, and the task must outlive a deleted conversation (same
reasoning as ``AgentPublish.model_config_id``).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Unicode, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc

TARGET_TYPES = ("script", "playwright", "agent")


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        # The scheduler's due-task scan.
        Index("ix_scheduled_tasks_due", "enabled", "next_run_at"),
        Index("ix_scheduled_tasks_project", "project_id"),
    )

    schedule_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False)
    name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Unicode(2000))

    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # script|playwright|agent
    script_id: Mapped[str | None] = mapped_column(
        ForeignKey("automation_scripts.script_id")
    )
    spec_slug: Mapped[str | None] = mapped_column(String(100))
    agent_slug: Mapped[str | None] = mapped_column(String(100))
    prompt: Mapped[str | None] = mapped_column(UnicodeText)
    # Playwright APP_* environment overrides as JSON; non-secret by design.
    env_json: Mapped[str | None] = mapped_column(Unicode(2000))

    cron_expr: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status: Mapped[str | None] = mapped_column(String(20))

    # Delivery target for the result summary. No FK on purpose (see module docstring).
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    notify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Owning user: result messages are attributed to them, and their id is what
    # script runs record in ``triggered_by`` so the live-log WS ownership check passes.
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    runs: Mapped[list["ScheduledTaskRun"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class ScheduledTaskRun(Base):
    __tablename__ = "scheduled_task_runs"
    __table_args__ = (
        Index("ix_scheduled_task_runs_schedule", "schedule_id", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    schedule_id: Mapped[str] = mapped_column(
        ForeignKey("scheduled_tasks.schedule_id"), nullable=False
    )
    # Denormalized for project-scoped history queries without a join.
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="schedule")  # schedule|manual
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending|running|completed|failed|skipped
    # The script_runs row this fire produced. No FK: script rows are deleted
    # with the project, and the history entry should survive that.
    script_run_id: Mapped[str | None] = mapped_column(String(36))
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    summary: Mapped[str | None] = mapped_column(Unicode(2000))
    error: Mapped[str | None] = mapped_column(Unicode(2000))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)

    task: Mapped["ScheduledTask"] = relationship(back_populates="runs")
