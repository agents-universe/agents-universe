"""knowledge_rw write must surface index failures instead of pretending.

A detail file whose reindex fails has no knowledge_metadata row — the
deferred list (built from the DB) then shows nothing, and the entry
"evaporates" from the agent's view even though the file exists on disk.
The write still succeeds (file kept, no rollback), but the result carries
`indexed: false` plus a warning the model can act on.
"""
from __future__ import annotations

from pathlib import Path

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


async def test_identical_content_without_db_reports_not_indexed(tmp_path):
    """Identical content still needs the row refreshed — the disk hash says
    nothing about whether the index row exists. With no session that cannot
    happen, so indexed is False and the warning matches a changed write."""
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
    assert result["indexed"] is False
    assert any("NOT updated" in w for w in result.get("warnings", []))


async def test_identical_content_still_reindexes(tmp_path, monkeypatch):
    """An unchanged write must refresh the DB row: a previous reindex may
    have failed (or the DB been rebuilt), so indexed=True is only truthful
    after reindex_one actually ran."""
    import agent_core.knowledge.index as index_mod

    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return {"action": "updated"}

    monkeypatch.setattr(index_mod, "reindex_one", _fake)

    tool = KnowledgeRWTool()
    ctx = _context(tmp_path, db=object())
    params = {"operation": "write", "slug": "domain/context", "content": "same content"}

    first = await tool.execute(params, ctx)
    second = await tool.execute(params, ctx)

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["indexed"] is True
    assert len(calls) == 2
    # The knowledge root travels with the call so a nested "knowledge"
    # directory cannot truncate the slug.
    assert Path(calls[-1]["knowledge_root"]).resolve() == (tmp_path / "knowledge").resolve()
