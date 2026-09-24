"""sql_query's reindex_knowledge must evict the process-level knowledge cache.

The cache's entries list is what feeds deferred_entries/status on the next
conversation. knowledge_rw and the REST reindex endpoints invalidate after
every successful index — sql_query's reindex_knowledge wrote the same DB rows
and never evicted, so stale metadata (title/summary/word_count) kept serving
until process restart.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import agent_core.knowledge.index as index_mod
from agent_core.knowledge.cache import CachedProjectKnowledge, KnowledgeCache
from agent_core.tools.base import ToolContext
from agent_core.tools.sql_query import SqlQueryTool


def _context(tmp_path, cache: KnowledgeCache) -> ToolContext:
    (tmp_path / "knowledge").mkdir(exist_ok=True)
    return ToolContext(
        project_id="p1",
        project_fs_path=str(tmp_path),
        conversation_id="c1",
        user_id="u1",
        db_session=AsyncMock(),
        knowledge_cache=cache,
    )


def _seed(cache: KnowledgeCache) -> None:
    cache._store["p1"] = CachedProjectKnowledge(project_id="p1", entries=[])


@pytest.mark.asyncio
async def test_full_reindex_invalidates_project_cache(tmp_path):
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "foo.md").write_text(
        "---\ntitle: Foo\n---\n\nBody.\n", encoding="utf-8"
    )
    cache = KnowledgeCache()
    _seed(cache)

    result = await SqlQueryTool().execute(
        {"operation": "reindex_knowledge"}, _context(tmp_path, cache)
    )

    assert result.get("success") is True, result
    assert "p1" not in cache._store, "full reindex left the stale entries cached"


@pytest.mark.asyncio
async def test_single_file_reindex_invalidates_cache(tmp_path, monkeypatch):
    async def _fake_reindex(fs_path, project_id, db_session):
        return {"action": "updated", "slug": "foo"}

    monkeypatch.setattr(index_mod, "reindex_one", _fake_reindex)
    cache = KnowledgeCache()
    _seed(cache)

    result = await SqlQueryTool().execute(
        {"operation": "reindex_knowledge", "params": {"fs_path": "foo.md"}},
        _context(tmp_path, cache),
    )

    assert result.get("action") == "updated", result
    assert "p1" not in cache._store, "single-file reindex left the stale entries cached"


@pytest.mark.asyncio
async def test_failed_single_file_reindex_keeps_cache(tmp_path, monkeypatch):
    """An error result means reindex_one wrote nothing — evicting would just
    force a pointless reload of the same rows."""
    async def _fake_reindex(fs_path, project_id, db_session):
        return {"error": "File not found: foo.md"}

    monkeypatch.setattr(index_mod, "reindex_one", _fake_reindex)
    cache = KnowledgeCache()
    _seed(cache)

    result = await SqlQueryTool().execute(
        {"operation": "reindex_knowledge", "params": {"fs_path": "foo.md"}},
        _context(tmp_path, cache),
    )

    assert "error" in result
    assert "p1" in cache._store
