"""Filesystem tool path normalization, not-found hints, and the tree operation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_core.paths import canonical
from agent_core.tools.base import ToolContext
from agent_core.tools.filesystem import FilesystemTool, _normalize_rel_path


def _base(tmp_path: Path) -> Path:
    b = tmp_path / "proj"
    b.mkdir()
    return b


def _ctx(project_root: Path, framework_root: Path | None = None) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=str(project_root),
        conversation_id="c1",
        user_id="u1",
        framework_root=str(framework_root) if framework_root is not None else None,
    )


# --- _normalize_rel_path -------------------------------------------------


def test_normalize_strips_absolute_prefix(tmp_path):
    base = _base(tmp_path)
    assert _normalize_rel_path(f"{base}/x/y.md", str(base)) == "x/y.md"


def test_normalize_strips_absolute_prefix_case_differs(tmp_path):
    base = _base(tmp_path)
    upper = canonical(base).as_posix().upper()
    assert _normalize_rel_path(f"{upper}/x/y.md", str(base)) == "x/y.md"


def test_normalize_strips_backslash_absolute(tmp_path):
    base = _base(tmp_path)
    assert _normalize_rel_path(str(base) + "\\x\\y.md", str(base)) == "x/y.md"


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows-only")
def test_normalize_strips_leading_slash_before_drive(tmp_path):
    base = _base(tmp_path)
    drive_form = "/" + canonical(base).as_posix() + "/x/y.md"
    assert _normalize_rel_path(drive_form, str(base)) == "x/y.md"


def test_normalize_strips_projects_slug_prefix(tmp_path):
    base = _base(tmp_path)
    assert _normalize_rel_path(f"projects/{base.name}/docs/a.md", str(base)) == "docs/a.md"


def test_normalize_projects_slug_not_stripped_when_real_dir_exists(tmp_path):
    base = _base(tmp_path)
    (base / "projects" / base.name).mkdir(parents=True)
    rel = f"projects/{base.name}/docs/a.md"
    assert _normalize_rel_path(rel, str(base)) == rel


def test_normalize_strips_bare_slug_prefix(tmp_path):
    base = _base(tmp_path)
    assert _normalize_rel_path(f"{base.name}/docs/a.md", str(base)) == "docs/a.md"


def test_normalize_bare_slug_not_stripped_when_real_dir_exists(tmp_path):
    base = _base(tmp_path)
    (base / base.name).mkdir()
    rel = f"{base.name}/docs/a.md"
    assert _normalize_rel_path(rel, str(base)) == rel


def test_normalize_bare_slug_case_insensitive_strip(tmp_path):
    base = _base(tmp_path)
    assert _normalize_rel_path(f"{base.name.upper()}/docs/a.md", str(base)) == "docs/a.md"


# --- not-found hints ------------------------------------------------------


async def test_read_not_found_middle_typo_lists_deepest_ancestor(tmp_path):
    base = _base(tmp_path)
    (base / "a").mkdir()
    (base / "a" / "b-real.txt").write_text("x", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": "a/b/c/file.md"}, _ctx(base)
    )

    assert "File not found" in result["error"]
    assert result["scope"] == "project"
    assert result["closest_existing_dir"] == "a"
    assert "b-real.txt" in result["available_in_parent"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="case-insensitive FS resolves the mismatch, so no not-found occurs",
)
async def test_read_not_found_case_mismatch_suggests(tmp_path):
    base = _base(tmp_path)
    (base / "Reports").mkdir()
    (base / "Reports" / "Summary.md").write_text("s", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": "reports/summary.md"}, _ctx(base)
    )

    assert result["did_you_mean"] == ["Reports/Summary.md"]


async def test_read_not_found_close_name_suggests(tmp_path):
    base = _base(tmp_path)
    (base / "Summary.md").write_text("s", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": "sumary.md"}, _ctx(base)
    )

    assert result["did_you_mean"] == ["Summary.md"]


async def test_read_not_found_scope_project_key(tmp_path):
    base = _base(tmp_path)
    tool = FilesystemTool()

    result = await tool.execute({"operation": "read_file", "path": "nope.md"}, _ctx(base))

    assert "File not found" in result["error"]
    assert result["scope"] == "project"


async def test_framework_not_found_scope_framework(tmp_path):
    base = _base(tmp_path)
    fw = tmp_path / "fw"
    (fw / "agents").mkdir(parents=True)
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "read_file", "path": "agents/missing.agent.md"}, _ctx(base, fw)
    )

    assert "File not found" in result["error"]
    assert result["scope"] == "framework"
    assert result["closest_existing_dir"] == "."


async def test_list_dir_not_found_uses_directory_label(tmp_path):
    base = _base(tmp_path)
    (base / "docs").mkdir()
    tool = FilesystemTool()

    result = await tool.execute(
        {"operation": "list_dir", "path": "docs/sub"}, _ctx(base)
    )

    assert "Directory not found" in result["error"]
    assert result["closest_existing_dir"] == "docs"


# --- tree -----------------------------------------------------------------


async def test_tree_default_depth_two_and_skips_noise(tmp_path):
    base = _base(tmp_path)
    (base / "src" / "pkg").mkdir(parents=True)
    (base / "src" / "main.py").write_text("x", encoding="utf-8")
    (base / "src" / "pkg" / "deep.py").write_text("x", encoding="utf-8")
    (base / "src" / "pkg" / "deeper").mkdir()
    (base / "src" / "pkg" / "deeper" / "far.py").write_text("x", encoding="utf-8")
    (base / "node_modules" / "left-pad").mkdir(parents=True)
    (base / ".git").mkdir()
    tool = FilesystemTool()

    result = await tool.execute({"operation": "tree", "path": "src"}, _ctx(base))

    text = result["tree"]
    assert text.splitlines()[0] == "src/"
    assert "main.py" in text
    assert "pkg/" in text
    assert "deep.py" in text
    assert "far.py" not in text  # depth 2 stops at pkg's children
    assert "node_modules" not in text
    assert ".git" not in text
    assert result["truncated"] is False
    assert result["entry_count"] == 4  # main.py, pkg/, deep.py, deeper/


async def test_tree_root_dot_lists_workspace_root(tmp_path):
    base = _base(tmp_path)
    (base / "docs").mkdir()
    (base / "docs" / "a.md").write_text("x", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute({"operation": "tree", "path": "."}, _ctx(base))

    assert result["path"] == "."
    assert "docs/" in result["tree"]
    assert "a.md" in result["tree"]


async def test_tree_depth_clamped(tmp_path):
    base = _base(tmp_path)
    p = base
    for name in ("l1", "l2", "l3"):
        p = p / name
        p.mkdir()
    (p / "l4.txt").write_text("x", encoding="utf-8")
    tool = FilesystemTool()

    r99 = await tool.execute({"operation": "tree", "path": ".", "depth": 99}, _ctx(base))
    assert "l4.txt" in r99["tree"]  # 99 clamps to max depth 4

    r0 = await tool.execute({"operation": "tree", "path": ".", "depth": 0}, _ctx(base))
    assert "l2/" in r0["tree"]  # 0 is falsy → default depth 2
    assert "l3" not in r0["tree"]


async def test_tree_truncation(tmp_path):
    base = _base(tmp_path)
    for i in range(250):
        (base / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute({"operation": "tree", "path": "."}, _ctx(base))

    assert result["truncated"] is True
    assert result["entry_count"] <= 200
    assert "truncated" in result["tree"]


async def test_tree_agents_falls_through_to_workspace(tmp_path):
    base = _base(tmp_path)
    fw = tmp_path / "fw"
    (base / "agents").mkdir()
    (base / "agents" / "local.agent.md").write_text("x", encoding="utf-8")
    (fw / "agents").mkdir(parents=True)
    (fw / "agents" / "global.agent.md").write_text("x", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute({"operation": "tree", "path": "agents"}, _ctx(base, fw))

    assert "local.agent.md" in result["tree"]
    assert "global.agent.md" not in result["tree"]


async def test_tree_not_a_directory_error(tmp_path):
    base = _base(tmp_path)
    (base / "a.txt").write_text("x", encoding="utf-8")
    tool = FilesystemTool()

    result = await tool.execute({"operation": "tree", "path": "a.txt"}, _ctx(base))

    assert "Not a directory" in result["error"]


async def test_tree_not_found_hint(tmp_path):
    base = _base(tmp_path)
    (base / "docs").mkdir()
    tool = FilesystemTool()

    result = await tool.execute({"operation": "tree", "path": "docs/sub"}, _ctx(base))

    assert "Directory not found" in result["error"]
    assert result["closest_existing_dir"] == "docs"
