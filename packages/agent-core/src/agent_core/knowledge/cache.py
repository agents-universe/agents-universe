"""Application-level knowledge cache — detail-level entries per project.

Only detail-level files are indexed in the database. This cache stores their
metadata (entries) so we don't re-query on every conversation. Content is
never pre-loaded — detail files are loaded on demand via knowledge_rw load.
Primary files are loaded directly from disk by the loader, bypassing this cache.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.knowledge.loader import KnowledgeEntry

_log = logging.getLogger("agent_core.knowledge.cache")


@dataclass
class CachedProjectKnowledge:
    project_id: str
    entries: list["KnowledgeEntry"]  # detail-level entries only (from DB)
    content: dict[str, str | None] = field(default_factory=dict)


class KnowledgeCache:
    """Process-level cache for parsed knowledge entries and file contents.

    Populated on first access per project (lazy). Thread-safe via per-project
    asyncio.Lock to prevent cache stampede when multiple concurrent conversations
    hit the same cold project.
    """

    def __init__(self) -> None:
        self._store: dict[str, CachedProjectKnowledge] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, project_id: str) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    async def get_or_load(
        self, project_id: str, db_session: Any
    ) -> CachedProjectKnowledge:
        """Return cached project knowledge, loading from DB+disk on first access."""
        if project_id in self._store:
            return self._store[project_id]

        async with self._lock_for(project_id):
            # Double-check after acquiring lock
            if project_id in self._store:
                return self._store[project_id]

            cached = await self._populate(project_id, db_session)
            self._store[project_id] = cached
            _log.debug(
                "Knowledge cache populated for project %s: %d entries, %d with content",
                project_id,
                len(cached.entries),
                sum(1 for v in cached.content.values() if v is not None),
            )
            return cached

    def invalidate(self, project_id: str) -> None:
        """Evict all cached data for a project (e.g. after re-indexing)."""
        self._store.pop(project_id, None)
        _log.debug("Knowledge cache invalidated for project %s", project_id)

    def invalidate_slug(self, project_id: str, slug: str) -> None:
        """Evict a single file's cached content so it is re-read on next access."""
        cached = self._store.get(project_id)
        if cached is None:
            return
        if slug in cached.content:
            del cached.content[slug]
            _log.debug("Knowledge cache entry evicted: %s / %s", project_id, slug)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _populate(
        self, project_id: str, db_session: Any
    ) -> CachedProjectKnowledge:
        from agent_core.knowledge.loader import _fetch_all_entries

        entries = await _fetch_all_entries(db_session, project_id)
        # All entries in DB are detail-level now; no content pre-loading needed
        return CachedProjectKnowledge(
            project_id=project_id,
            entries=entries,
            content={},
        )
