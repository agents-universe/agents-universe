"""Project model."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Unicode, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_project_slug"),
    )

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id"))
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Unicode(2000))
    category: Mapped[str] = mapped_column(
        Unicode(50), nullable=False, default="software", server_default="software"
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    created_by: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # "public" — anyone can access; "private" — creator + project_members only
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default="public", server_default="public"
    )

    children: Mapped[list["Project"]] = relationship(back_populates="parent")
    parent: Mapped["Project | None"] = relationship(back_populates="children", remote_side=[project_id])
