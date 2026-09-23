"""knowledge_rw write must surface index failures instead of pretending.

A detail file whose reindex fails has no knowledge_metadata row — the
deferred list (built from the DB) then shows nothing, and the entry
"evaporates" from the agent's view even though the file exists on disk.
The write still succeeds (file kept, no rollback), but the result carries
`indexed: false` plus a warning the model can act on.
"""
from __future__ import annotations

from agent_core.tools.base import ToolContext
from agent_core.tools.knowledge_rw import KnowledgeRWTool


def _context(tmp_path, db=None):
    return ToolContext(
        project_id="p1",
        project_fs_path=str(tmp_path),
        conversation_id="conv",
        user_id="u1",
        db_session=db,
        knowledge_cache=None,
        session=None,
    )


async def test_write_without_db_session_reports_not_indexed(tmp_path):
    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, db=None)

    result = await tool.execute(
        {"operation": "write", "slug": "technical/api/users", "content": "detail body"},
        ctx,
    )

    assert result["success"] is True
    assert result["changed"] is True
    assert result["indexed"] is False
    assert any("NOT updated" in w for w in result.get("warnings", []))
    # File kept — no rollback.
    assert (tmp_path / "knowledge" / "technical" / "api" / "users.md").exists()


async def test_write_reindex_exception_reports_not_indexed(tmp_path, monkeypatch):
    import agent_core.knowledge.index as index_mod

    async def _boom(**kwargs):
        raise RuntimeError("reindex exploded")

    monkeypatch.setattr(index_mod, "reindex_one", _boom)

    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, db=object())  # any non-None session

    result = await tool.execute(
        {"operation": "write", "slug": "technical/api/users", "content": "detail body"},
        ctx,
    )

    assert result["success"] is True
    assert result["changed"] is True
    assert result["indexed"] is False
    assert any("NOT updated" in w for w in result.get("warnings", []))


async def test_write_reindex_error_dict_reports_not_indexed(tmp_path, monkeypatch):
    """reindex_one returns {"error": ...} instead of raising (e.g. models missing)."""
    import agent_core.knowledge.index as index_mod

    async def _error(**kwargs):
        return {"error": "DB models not available"}

    monkeypatch.setattr(index_mod, "reindex_one", _error)

    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, db=object())

    result = await tool.execute(
        {"operation": "write", "slug": "technical/api/x", "content": "body"},
        ctx,
    )

    assert result["indexed"] is False
    assert any("NOT updated" in w for w in result.get("warnings", []))


async def test_identical_content_reports_indexed(tmp_path):
    """Unchanged content needs no reindex — reported as indexed, no warnings."""
    target = tmp_path / "knowledge" / "domain" / "context.md"
    target.parent.mkdir(parents=True)
    target.write_text("same content", encoding="utf-8")

    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, db=None)

    result = await tool.execute(
        {"operation": "write", "slug": "domain/context", "content": "same content"},
        ctx,
    )

    assert result["changed"] is False
    assert result["indexed"] is True
    assert "warnings" not in result
