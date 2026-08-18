"""Tests for test_generator output_dir confinement."""
from __future__ import annotations

from agent_core.tools.base import ToolContext
from agent_core.tools.test_generator import TestGeneratorTool


def _context(project_fs_path) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=str(project_fs_path),
        conversation_id="c1",
        user_id="u1",
    )


def _params(**overrides) -> dict:
    base = {
        "operation": "generate_spec",
        "issue_key": "PROJ-1",
        "test_cases": [{"title": "t", "steps": ["wait"]}],
    }
    base.update(overrides)
    return base


async def test_output_dir_escape_rejected(tmp_path):
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_a.mkdir()
    proj_b.mkdir()
    tool = TestGeneratorTool()
    result = await tool.execute(_params(output_dir="../proj-b/x"), _context(proj_a))
    assert "error" in result
    assert not (proj_b / "x").exists()


async def test_default_output_dir_unaffected(tmp_path):
    proj_a = tmp_path / "proj-a"
    proj_a.mkdir()
    tool = TestGeneratorTool()
    result = await tool.execute(_params(), _context(proj_a))
    assert result.get("success") is True
    assert (proj_a / "tests" / "generated" / "proj-1.spec.ts").exists()
