"""Project graph endpoints: namespacing, needs_build, docs, auth.

The workspace layout mirrors production: clones under {ws}/repos/<name>/
(.git marker) and graphs under {ws}/.tmp/repo_graph/<name>/graph.json.
"""
from __future__ import annotations

import json

import pytest

from agent_core.knowledge.graph.model import (
    EDGE_CALLS,
    EDGE_IMPORTS,
    GraphEdge,
    GraphNode,
    RepoGraph,
    RepoMeta,
)
from api.models.knowledge import KnowledgeMetadata
from api.paths import PROJECTS_ROOT


def _seed_repo(ws, name: str, graph: RepoGraph | None) -> None:
    (ws / "repos" / name / ".git").mkdir(parents=True, exist_ok=True)
    if graph is not None:
        kg_dir = ws / ".tmp" / "repo_graph" / name
        kg_dir.mkdir(parents=True, exist_ok=True)
        (kg_dir / "graph.json").write_text(json.dumps(graph.to_dict()), encoding="utf-8")


def _api_graph() -> RepoGraph:
    return RepoGraph(
        repo=RepoMeta(name="api", head_sha="abc", langs=["python"], built_at=1.0),
        stats={"files": 2, "nodes": 4, "edges": 2},
        nodes=[
            GraphNode(id="f:src/x.py", type="file", name="src/x.py", lang="python"),
            GraphNode(id="f:src/y.py", type="file", name="src/y.py", lang="python"),
            GraphNode(id="s:src/x.py:foo", type="function", name="foo", line=3),
            GraphNode(id="s:src/y.py:bar", type="function", name="bar", line=7),
        ],
        edges=[
            GraphEdge(src="f:src/x.py", dst="f:src/y.py", type=EDGE_IMPORTS),
            GraphEdge(src="s:src/x.py:foo", dst="s:src/y.py:bar", type=EDGE_CALLS),
        ],
    )


def _doc_row(project_id: str, slug: str, cross_references: list[str]) -> KnowledgeMetadata:
    return KnowledgeMetadata(
        project_id=project_id,
        category=slug.split("/")[0],
        slug=slug,
        title=f"Title {slug}",
        fs_path=f"/nonexistent/{slug}.md",
        knowledge_level="detail",
        cross_references=json.dumps(cross_references),
        summary=f"Summary {slug}",
    )


@pytest.fixture
def ws():
    """The test PROJECTS_ROOT conftest already points api.paths at."""
    return PROJECTS_ROOT


async def test_empty_project_graph(client, make_project, ws):
    project = await make_project(slug="graph-empty")
    resp = await client.get(f"/api/projects/{project.project_id}/graph")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_build"] is False
    assert body["no_repos"] is True
    assert body["nodes"] == [] and body["edges"] == []
    assert body["repos"] == []


async def test_graph_namespacing_docs_and_needs_build(client, make_project, db, ws):
    project = await make_project(slug="graph-basic")
    ws_slug = ws / "graph-basic"
    _seed_repo(ws_slug, "api", _api_graph())
    _seed_repo(ws_slug, "web", None)  # clone without a built graph
    db.add(_doc_row(project.project_id, "domain/context", ["technical/api", "missing"]))
    db.add(_doc_row(project.project_id, "technical/api", []))
    await db.commit()

    resp = await client.get(f"/api/projects/{project.project_id}/graph")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_build"] is False
    assert body["no_repos"] is False
    by_id = {node["id"]: node for node in body["nodes"]}
    assert by_id["api:f:src/x.py"]["repo"] == "api"
    assert "web:f:src/x.py" not in by_id  # web has no graph.json
    assert {repo["name"]: repo["has_graph"] for repo in body["repos"]} == {
        "api": True, "web": False,
    }
    # Docs merge as module nodes; references edges only between known slugs.
    assert by_id["d:domain/context"]["type"] == "module"
    assert by_id["d:domain/context"]["repo"] is None
    refs = {(e["src"], e["dst"]) for e in body["edges"] if e["type"] == "references"}
    assert refs == {("d:domain/context", "d:technical/api")}
    # Overview: symbols dropped, the calls edge rolled up to a file-file edge.
    assert "api:s:src/x.py:foo" not in by_id
    calls = {(e["src"], e["dst"]) for e in body["edges"] if e["type"] == EDGE_CALLS}
    assert calls == {("api:f:src/x.py", "api:f:src/y.py")}
    assert body["counts"]["docs"] == 2 and body["counts"]["repos"] == 1


async def test_needs_build_when_no_graphs(client, make_project, ws):
    project = await make_project(slug="graph-unbuilt")
    _seed_repo(ws / "graph-unbuilt", "fresh", None)
    resp = await client.get(f"/api/projects/{project.project_id}/graph")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_build"] is True
    assert body["no_repos"] is False
    assert body["nodes"] == []


async def test_full_level_and_invalid_level(client, make_project, ws):
    project = await make_project(slug="graph-levels")
    _seed_repo(ws / "graph-levels", "api", _api_graph())

    resp = await client.get(
        f"/api/projects/{project.project_id}/graph?level=full"
    )
    ids = {node["id"] for node in resp.json()["nodes"]}
    assert "api:s:src/x.py:foo" in ids

    resp = await client.get(
        f"/api/projects/{project.project_id}/graph?level=deep"
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "GRAPH_LEVEL_INVALID"


async def test_neighbors_endpoint(client, make_project, ws):
    project = await make_project(slug="graph-nbr")
    _seed_repo(ws / "graph-nbr", "api", _api_graph())

    resp = await client.get(
        f"/api/projects/{project.project_id}/graph/neighbors",
        params={"node": "api:s:src/x.py:foo"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["node"]["id"] == "api:s:src/x.py:foo"
    assert {entry["id"] for entry in body["neighbors"]} >= {"api:s:src/y.py:bar"}
    assert all(entry["repo"] == "api" for entry in body["neighbors"])

    resp = await client.get(
        f"/api/projects/{project.project_id}/graph/neighbors",
        params={"node": "api:f:ghost.py"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "GRAPH_NODE_NOT_FOUND"

    resp = await client.get(
        f"/api/projects/{project.project_id}/graph/neighbors",
        params={"node": "d:domain/context"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "GRAPH_NODE_INVALID"

    resp = await client.get(
        f"/api/projects/{project.project_id}/graph/neighbors",
        params={"node": "no-colon"},
    )
    assert resp.status_code == 400


async def test_build_endpoint_reports_per_repo_status(client, make_project, ws):
    project = await make_project(slug="graph-build")
    _seed_repo(ws / "graph-build", "tiny", None)
    resp = await client.post(
        f"/api/projects/{project.project_id}/graph/build", json={"force": False}
    )
    # The clone has no git history / no graphify in every environment: the
    # exact status varies (failed/skipped/built) but the endpoint must answer
    # 200 with one summary per clone — never a 500.
    assert resp.status_code == 200, resp.text
    repos = resp.json()["repos"]
    assert [entry["name"] for entry in repos] == ["tiny"]
    assert "status" in repos[0]


async def test_private_project_denied(client, make_project, as_user, ws):
    project = await make_project(slug="graph-private", created_by="test-user")
    patch = await client.patch(
        f"/api/projects/{project.project_id}", json={"visibility": "private"}
    )
    assert patch.status_code == 200, patch.text
    async with as_user("other-user"):
        resp = await client.get(f"/api/projects/{project.project_id}/graph")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "PROJECT_PRIVATE"
