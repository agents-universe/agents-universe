"""User-configured IMAP/SMTP email server integrations."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Integer, String, Unicode, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


class EmailServer(Base):
    """One mail account the `email` tool can read from / send through.

    User-scoped (Settings → Integrations → 邮件服务器), one row per
    (user_id, slug). The password is AES-256-GCM encrypted with the same
    scheme as user_tokens (token_vault.encrypt(plaintext, user_id));
    only key_hint is ever returned by APIs.
    """
    __tablename__ = "email_servers"
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_email_server_slug"),
    )

    email_server_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str | None] = mapped_column(Unicode(255))
    imap_host: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, default=993)
    # ssl | starttls | none
    imap_security: Mapped[str] = mapped_column(String(10), nullable=False, default="ssl")
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    smtp_security: Mapped[str] = mapped_column(String(10), nullable=False, default="starttls")
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_password: Mapped[str] = mapped_column(String(4000), nullable=False)
    key_hint: Mapped[str | None] = mapped_column(String(10))
    # Internal mail servers live on private IPs — opt out of the SSRF
    # private-IP block per server (same escape hatch as MCP catalog).
    allow_private_network: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
