"""Tests for the knowledge_rw write placement note.

When a write creates a brand-new slug, the result carries an advisory
(non-blocking) note listing the project's existing knowledge files and
asking the agent to merge into one of them instead — the runtime nudge
behind the "fill existing templates first" priority. Legitimate new files
(two-level API/Kong detail files, hierarchy children with `parent`
frontmatter) are exempt.
"""
from __future__ import annotations

from agent_core.knowledge.loader import KnowledgeContextResult
from agent_core.tools.base import ToolContext
from agent_core.tools.knowledge_rw import KnowledgeRWTool


def _context(tmp_path, db=None, project_ctx=None):
    return ToolContext(
        project_id="p1",
        project_fs_path=str(tmp_path),
        conversation_id="conv",
        user_id="u1",
        db_session=db,
        knowledge_cache=None,
        session=None,
        project_context=project_ctx,
    )


async def test_new_flat_slug_gets_placement_note_lists_existing_files(tmp_path):
    existing = tmp_path / "knowledge" / "domain" / "context.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("---\ntitle: Context\n---\nbody", encoding="utf-8")

    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=None)

    result = await tool.execute(
        {"operation": "write", "slug": "technical/foo", "content": "new content"}, context
    )

    assert result["changed"] is True
    assert "note" in result
    assert "'domain/context'" in result["note"]
    assert "merge it into that file" in result["note"]
    assert (tmp_path / "knowledge" / "technical" / "foo.md").exists()  # write not blocked


async def test_update_existing_slug_no_placement_note(tmp_path):
    target = tmp_path / "knowledge" / "technical" / "foo.md"
    target.parent.mkdir(parents=True)
    target.write_text("old content", encoding="utf-8")

    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=None)

    result = await tool.execute(
        {"operation": "write", "slug": "technical/foo", "content": "new content"}, context
    )

    assert result["changed"] is True
    assert "note" not in result


async def test_technical_api_detail_slug_exempt(tmp_path):
    existing = tmp_path / "knowledge" / "domain" / "context.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("body", encoding="utf-8")

    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=None)

    result = await tool.execute(
        {"operation": "write", "slug": "technical/api/users-service", "content": "detail"}, context
    )

    assert result["changed"] is True
    assert "note" not in result


async def test_slug_with_parent_frontmatter_exempt(tmp_path):
    existing = tmp_path / "knowledge" / "domain" / "context.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("body", encoding="utf-8")

    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=None)
    content = "---\ntitle: Child\nparent: technical/api-map\n---\nbody"

    result = await tool.execute(
        {"operation": "write", "slug": "technical/foo", "content": content}, context
    )

    assert result["changed"] is True
    assert "note" not in result


async def test_note_uses_project_context_when_disk_empty(tmp_path):
    ctx = KnowledgeContextResult()
    ctx.loaded_content["domain/context"] = "context content"

    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=None, project_ctx=ctx)

    result = await tool.execute(
        {"operation": "write", "slug": "technical/foo", "content": "new content"}, context
    )

    assert result["changed"] is True
    assert "note" in result
    assert "'domain/context'" in result["note"]


async def test_no_note_when_project_has_no_knowledge(tmp_path):
    tool = KnowledgeRWTool()
    context = _context(tmp_path, db=None)

    result = await tool.execute(
        {"operation": "write", "slug": "technical/foo", "content": "new content"}, context
    )

    assert result["changed"] is True
    assert "note" not in result
