"""knowledge_rw read with `slugs` — several files in one call.

Files that are not `knowledge_level: detail` are already in the system prompt,
so a read is for the detail files an agent actually still needs; asking for
three of them used to cost three turns.
"""
from __future__ import annotations

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.knowledge_rw import KnowledgeRWTool


def _context(tmp_path) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=str(tmp_path),
        conversation_id="conv",
        user_id="u1",
        db_session=None,
        knowledge_cache=None,
        session=None,
        project_context=None,
    )


def _seed(tmp_path, slug: str, body: str) -> None:
    path = tmp_path / "knowledge" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {slug}\n---\n{body}", encoding="utf-8")


@pytest.mark.asyncio
async def test_read_slugs_returns_every_file_in_order(tmp_path):
    _seed(tmp_path, "api-map", "endpoints")
    _seed(tmp_path, "domain/orders", "order rules")

    result = await KnowledgeRWTool().execute(
        {"operation": "read", "slugs": ["api-map", "domain/orders"]},
        _context(tmp_path),
    )

    assert result["count"] == 2
    assert [f["slug"] for f in result["files"]] == ["api-map", "domain/orders"]
    assert "endpoints" in result["files"][0]["content"]
    assert result["files"][1]["metadata"]["title"] == "domain/orders"


@pytest.mark.asyncio
async def test_read_slugs_reports_a_missing_file_without_sinking_the_batch(tmp_path):
    _seed(tmp_path, "api-map", "endpoints")

    result = await KnowledgeRWTool().execute(
        {"operation": "read", "slugs": ["api-map", "nope"]},
        _context(tmp_path),
    )

    assert result["count"] == 2
    assert "endpoints" in result["files"][0]["content"]
    assert "not found" in result["files"][1]["error"]


@pytest.mark.asyncio
async def test_read_slugs_accepts_a_stringified_list(tmp_path):
    _seed(tmp_path, "a", "one")
    _seed(tmp_path, "b", "two")

    result = await KnowledgeRWTool().execute(
        {"operation": "read", "slugs": "a, b"},
        _context(tmp_path),
    )

    assert [f["slug"] for f in result["files"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_read_slugs_rejects_an_empty_list(tmp_path):
    result = await KnowledgeRWTool().execute(
        {"operation": "read", "slugs": ["", "  "]},
        _context(tmp_path),
    )

    assert result["error"] == "slugs must be a non-empty array of slugs"


@pytest.mark.asyncio
async def test_read_singular_slug_still_works(tmp_path):
    _seed(tmp_path, "api-map", "endpoints")

    result = await KnowledgeRWTool().execute(
        {"operation": "read", "slug": "api-map"},
        _context(tmp_path),
    )

    assert result["slug"] == "api-map"
    assert "endpoints" in result["content"]


@pytest.mark.asyncio
async def test_read_prefers_the_singular_slug_when_both_are_passed(tmp_path):
    """A model that fills in both fields gets the plain read, not a batch."""
    _seed(tmp_path, "api-map", "endpoints")
    _seed(tmp_path, "other", "more")

    result = await KnowledgeRWTool().execute(
        {"operation": "read", "slug": "api-map", "slugs": ["other"]},
        _context(tmp_path),
    )

    assert result["slug"] == "api-map"
    assert "files" not in result
