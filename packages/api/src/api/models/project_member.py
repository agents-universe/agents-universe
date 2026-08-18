"""Project membership — users whitelisted to access a private project.

The creator (projects.created_by) has implicit access and is never stored
here; rows in this table are additional managers who can access a private
project and manage the whitelist itself.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._compat import UTCDateTime, now_utc as _now_utc


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.project_id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    added_by: Mapped[str] = mapped_column(String(100), nullable=False)
    # nullable matches migration x6i9l7h0i568 (column created nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=_now_utc)
