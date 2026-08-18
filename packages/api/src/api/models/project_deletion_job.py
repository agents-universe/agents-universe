"""Durable state for project workspace deletion and recovery."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class ProjectDeletionJob(Base):
    __tablename__ = "project_deletion_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_path: Mapped[str] = mapped_column(Unicode(1000), nullable=False)
    trash_path: Mapped[str] = mapped_column(Unicode(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="prepared")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Unicode(2000))
    prepared_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    quarantined_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    db_deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc, onupdate=_now_utc, nullable=False)
