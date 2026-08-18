"""Knowledge metadata and version models."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, Unicode, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


def _load_json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


class KnowledgeMetadata(Base):
    __tablename__ = "knowledge_metadata"

    knowledge_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id"))  # NULL = global
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    fs_path: Mapped[str] = mapped_column(Unicode(500), nullable=False)
    completeness_score: Mapped[float] = mapped_column(Float, default=0.0)
    coverage_breadth: Mapped[float] = mapped_column(Float, default=0.0)
    recency_score: Mapped[float] = mapped_column(Float, default=0.0)
    cross_ref_density: Mapped[float] = mapped_column(Float, default=0.0)
    agent_gap_score: Mapped[float] = mapped_column(Float, default=0.0)
    tags: Mapped[str | None] = mapped_column(Unicode(1000))             # JSON string array
    cross_references: Mapped[str | None] = mapped_column(String(2000)) # JSON slug array
    content_hash: Mapped[str | None] = mapped_column(String(64))
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Hierarchy fields
    knowledge_level: Mapped[str] = mapped_column(String(20), default="auto")  # "root" | "detail" | "auto"
    parent_slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    children_slugs: Mapped[str | None] = mapped_column(String(4000), nullable=True)  # JSON array
    summary: Mapped[str | None] = mapped_column(Unicode(500), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    versions: Mapped[list["KnowledgeVersion"]] = relationship(back_populates="knowledge")

    __table_args__ = (
        Index("ix_knowledge_level_project", "project_id", "knowledge_level"),
        Index("ix_knowledge_parent", "parent_slug"),
        # unique — concurrent index_directory/reindex_one INSERT
        # races otherwise silently duplicate (project_id, slug) rows, and
        # downstream scalar_one_or_none() reads (reindex_one, purge_residue)
        # then raise MultipleResultsFound.
        Index("ix_knowledge_project_slug", "project_id", "slug", unique=True),
        Index("ix_knowledge_project_depth", "project_id", "depth"),
    )

    # JSON string columns deserialized once, reused by routers/services
    @property
    def tags_list(self) -> list:
        return _load_json_list(self.tags)

    @property
    def children_list(self) -> list:
        return _load_json_list(self.children_slugs)

    @property
    def cross_references_list(self) -> list:
        return _load_json_list(self.cross_references)


class KnowledgeVersion(Base):
    __tablename__ = "knowledge_versions"

    version_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    knowledge_id: Mapped[str] = mapped_column(ForeignKey("knowledge_metadata.knowledge_id", ondelete="CASCADE"), nullable=False)
    version_num: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(UnicodeText, nullable=False)
    changed_by: Mapped[str | None] = mapped_column(Unicode(100))
    change_summary: Mapped[str | None] = mapped_column(Unicode(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)

    knowledge: Mapped["KnowledgeMetadata"] = relationship(back_populates="versions")


class KnowledgeLoadEvent(Base):
    """Tracks dynamic knowledge load/unload events for analytics."""

    __tablename__ = "knowledge_load_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    knowledge_id: Mapped[str] = mapped_column(ForeignKey("knowledge_metadata.knowledge_id"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "load" | "unload"
    reason: Mapped[str | None] = mapped_column(String(50))  # "agent_request" | "task_end"
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
