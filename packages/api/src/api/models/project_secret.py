"""Project-level secret storage — secrets belong to a project, not a user."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Unicode, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class ProjectSecret(Base):
    __tablename__ = "project_secrets"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "service_key", "environment", "secret_name",
            name="uq_project_secret_key",
        ),
    )

    secret_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False)
    service_key: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str | None] = mapped_column(String(100))
    secret_name: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    display_name: Mapped[str | None] = mapped_column(Unicode(255))
    encrypted_value: Mapped[str] = mapped_column(String(4000), nullable=False)
    key_hint: Mapped[str | None] = mapped_column(String(10))
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
