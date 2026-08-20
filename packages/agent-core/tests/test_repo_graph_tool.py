"""RepoGraphTool: all operations, repo resolution, traversal guards."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_core.knowledge.graph.languages import get_grammar
from agent_core.tools.base import ToolContext
from agent_core.tools.registry import build_tool_registry
from agent_core.tools.repo_graph import RepoGraphTool

SAMPLE = {
    "main.py": (
        "from lib.util import parse_json\n"
        "\n"
        "def main():\n"
        "    return parse_json('{}')\n"
    ),
    "lib/util.py": (
        "def parse_json(text):\n"
        "    return text\n"
    ),
}


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def make_context(project: Path) -> ToolContext:
    return ToolContext(
        project_id="proj",
        project_fs_path=str(project),
        conversation_id="conv",
        user_id="user-1",
        db_session=None,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Workspace with one committed checkout at repos/sample (no graph yet)."""
    proj = tmp_path / "proj"
    repo = proj / "repos" / "sample"
    for rel, text in SAMPLE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _run("init", "-b", "main", cwd=repo)
    _run("config", "user.email", "t@t.t", cwd=repo)
    _run("config", "user.name", "t", cwd=repo)
    _run("add", ".", cwd=repo)
    _run("commit", "-m", "seed", cwd=repo)
    return proj


@pytest.fixture
def grammars():
    if get_grammar("python") is None:
        pytest.skip("tree-sitter python grammar unavailable (offline?)")
    return True


async def _build(project: Path) -> dict:
    return await RepoGraphTool().execute(
        {"operation": "build"}, make_context(project)
    )


@pytest.mark.asyncio
async def test_build_then_all_ops(project: Path, grammars):
    tool = RepoGraphTool()
    ctx = make_context(project)

    result = await tool.execute({"operation": "build"}, ctx)
    assert result["status"] == "built"
    assert result["stats"]["files"] == 2
    assert "repo_map" in result and "repo=sample" in result["repo_map"]
    assert result["graph_path"].endswith("graph.json")

    # default force=True: an explicit build always re-checks the working tree
    again = await tool.execute({"operation": "build"}, ctx)
    assert again["status"] == "built"
    assert again["stats"]["reused"] == 2  # nothing changed -> cache hits

    query = await tool.execute(
        {"operation": "query", "repository": "sample", "query": "parse"}, ctx)
    assert any(m["name"] == "parse_json" for m in query["matches"])

    nb = await tool.execute(
        {"operation": "neighbors", "symbol": "main"}, ctx)
    assert any(n["name"] == "parse_json" for n in nb["neighbors"])

    impact = await tool.execute(
        {"operation": "impact", "symbol": "parse_json"}, ctx)
    assert impact["affected_files"] == ["main.py"]

    path = await tool.execute(
        {"operation": "path", "from": "main", "to": "parse_json"}, ctx)
    assert path["hops"] == 1
    assert path["path"][-1]["node_id"] == "s:lib/util.py:parse_json"

    report = await tool.execute({"operation": "report"}, ctx)
    assert "repo_map" in report
    assert report["report_path"].endswith("graph_report.md")
    # the full report never enters context
    assert "god nodes" not in report["repo_map"].lower()

    # repository_path resolves the same checkout
    via_path = await tool.execute(
        {"operation": "report", "repository_path": "repos/sample"}, ctx)
    assert via_path["repo_map"] == report["repo_map"]


@pytest.mark.asyncio
async def test_single_clone_fallback(project: Path, grammars):
    """With exactly one clone, omitting repository resolves it implicitly."""
    await _build(project)
    result = await RepoGraphTool().execute(
        {"operation": "query", "query": "parse"}, make_context(project))
    assert result.get("matches")


@pytest.mark.asyncio
async def test_no_graph_hint(project: Path):
    result = await RepoGraphTool().execute(
        {"operation": "query", "repository": "sample", "query": "parse"},
        make_context(project))
    assert "error" in result and "No graph" in result["error"]
    assert "hint" in result
    assert "graph_path" in result


@pytest.mark.asyncio
async def test_path_traversal_blocked(project: Path):
    tool = RepoGraphTool()
    ctx = make_context(project)
    for params in (
        {"operation": "query", "repository_path": "../evil", "query": "x"},
        {"operation": "query", "repository_path": "/abs/evil", "query": "x"},
        {"operation": "query", "repository_path": "C:/evil", "query": "x"},
    ):
        result = await tool.execute(params, ctx)
        assert "error" in result, params
    # an absolute-like repository name also cannot escape the repos dir
    result = await tool.execute(
        {"operation": "query", "repository": "../evil", "query": "x"}, ctx)
    assert "error" in result


@pytest.mark.asyncio
async def test_unknown_operation_and_missing_params(project: Path):
    tool = RepoGraphTool()
    ctx = make_context(project)
    # build first so the missing-param branch (not the no-graph hint) triggers
    await tool.execute({"operation": "build"}, ctx)
    bogus = await tool.execute({"operation": "bogus"}, ctx)
    assert "Unknown operation" in bogus["error"]
    empty = await tool.execute({"operation": "query", "repository": "sample"}, ctx)
    assert "query" in empty["error"].lower()


@pytest.mark.asyncio
async def test_registry_resolves(project: Path):
    registry = build_tool_registry(["repo_graph"])
    assert "repo_graph" in registry
    assert isinstance(registry["repo_graph"], RepoGraphTool)
