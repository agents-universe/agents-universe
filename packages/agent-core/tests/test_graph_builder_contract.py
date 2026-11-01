"""External contract of the graphify-backed repo_graph engine.

test_repo_graph_tool.py covers the happy-path ops; this suite pins the
semantics that must survive any engine swap: ambiguity is reported with
candidates (never guessed), compact_map stays inside the WS token budget,
graph.json remains schema v2, and FK/read edges keep their direction.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_core.knowledge.graph.model import SCHEMA_VERSION
from agent_core.knowledge.graph.queries import resolve_node
from agent_core.knowledge.graph.report import COMPACT_MAP_MAX_CHARS
from agent_core.knowledge.graph.store import load_cached
from agent_core.tools.base import ToolContext
from agent_core.tools.repo_graph import RepoGraphTool

# Two files export the same qname suffix — resolve_node must refuse to pick.
AMBIGUOUS = {
    "app/handlers.py": "def load():\n    return 'app'\n",
    "admin/handlers.py": "def load():\n    return 'admin'\n",
    "schema.sql": (
        "CREATE TABLE users (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    name TEXT\n"
        ");\n"
        "CREATE TABLE orders (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    user_id INTEGER REFERENCES users(id)\n"
        ");\n"
        "CREATE VIEW active_users AS SELECT id FROM users;\n"
    ),
}


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def graphify_available():
    try:
        import graphify.extract  # noqa: F401
    except ImportError:
        pytest.skip("graphifyy not installed")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    repo = proj / "repos" / "mixed"
    for rel, text in AMBIGUOUS.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _run("init", "-b", "main", cwd=repo)
    _run("config", "user.email", "t@t.t", cwd=repo)
    _run("config", "user.name", "t", cwd=repo)
    _run("add", ".", cwd=repo)
    _run("commit", "-m", "seed", cwd=repo)
    return proj


def _ctx(project: Path) -> ToolContext:
    return ToolContext(
        project_id="proj", project_fs_path=str(project),
        conversation_id="c", user_id="u", db_session=None,
    )


@pytest.mark.asyncio
async def test_ambiguous_symbol_reports_candidates_not_a_guess(
    project: Path, graphify_available
):
    tool = RepoGraphTool()
    ctx = _ctx(project)
    built = await tool.execute({"operation": "build"}, ctx)
    assert built["status"] == "built"

    graph = load_cached(Path(project) / ".tmp" / "repo_graph" / "mixed")
    assert graph is not None
    resolved = resolve_node(graph, "load")
    assert "error" in resolved and "ambiguous" in resolved["error"]
    assert len(resolved["ambiguous"]) == 2
    # both candidates present, so the caller can disambiguate by file
    assert {cid.split(":")[1] for cid in resolved["ambiguous"]} == {
        "app/handlers.py", "admin/handlers.py",
    }

    # the neighbors op surfaces the same refusal instead of traversing
    nb = await tool.execute({"operation": "neighbors", "symbol": "load"}, ctx)
    assert "error" in nb and "ambiguous" in nb["error"]
    assert "neighbors" not in nb

    # disambiguation is by full node id (qnames carry no module prefix)
    ok = await tool.execute(
        {"operation": "neighbors", "symbol": "s:app/handlers.py:load"}, ctx)
    assert "error" not in ok
    assert ok["node"]["name"] == "load"


@pytest.mark.asyncio
async def test_sql_fk_and_view_edges_keep_direction(project: Path, graphify_available):
    tool = RepoGraphTool()
    ctx = _ctx(project)
    await tool.execute({"operation": "build"}, ctx)

    graph = load_cached(Path(project) / ".tmp" / "repo_graph" / "mixed")
    assert graph is not None
    edges = {(e.src, e.dst, e.type) for e in graph.edges}

    # orders.user_id REFERENCES users(id): orders -> users, never reversed
    fk = [e for e in edges if e[2] == "foreign_key"]
    assert fk, "no foreign_key edge extracted"
    for src, dst, _ in fk:
        assert "orders" in src and "users" in dst, f"FK direction flipped: {src}->{dst}"

    # the view reads from users
    reads = [e for e in edges if e[2] == "reads"]
    assert any("active_users" in src and "users" in dst for src, dst, _ in reads), (
        f"no active_users -reads-> users edge; reads edges: {reads}"
    )

    # tables land as NODE_TABLE symbols, not files
    tables = {n.name for n in graph.nodes if n.type == "table"}
    assert {"users", "orders"} <= tables


@pytest.mark.asyncio
async def test_graph_json_is_schema_v2(project: Path, graphify_available):
    tool = RepoGraphTool()
    ctx = _ctx(project)
    result = await tool.execute({"operation": "build"}, ctx)
    assert result["status"] == "built"

    data = json.loads(Path(result["graph_path"]).read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    assert set(data) >= {"schema_version", "repo", "stats", "nodes", "edges"}
    for node in data["nodes"]:
        assert node["id"].startswith(("f:", "s:")), node
    # load_cached round-trips the file the tool just wrote
    graph = load_cached(Path(project) / ".tmp" / "repo_graph" / "mixed")
    assert graph is not None and graph.schema_version == SCHEMA_VERSION
    assert len(graph.nodes) == len(data["nodes"])


@pytest.mark.asyncio
async def test_compact_map_stays_inside_token_budget(project: Path, graphify_available):
    tool = RepoGraphTool()
    ctx = _ctx(project)
    built = await tool.execute({"operation": "build"}, ctx)
    assert len(built["repo_map"]) <= COMPACT_MAP_MAX_CHARS

    report = await tool.execute({"operation": "report"}, ctx)
    assert len(report["repo_map"]) <= COMPACT_MAP_MAX_CHARS
    # the full report stays on disk, out of context
    assert Path(report["report_path"]).is_file()
    assert "god nodes" not in report["repo_map"].lower()
