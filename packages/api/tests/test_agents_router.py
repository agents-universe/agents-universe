"""Router tests: project_id filtering, lazy sync, explicit sync endpoint."""
from __future__ import annotations

from api.paths import PROJECTS_ROOT


def _write_project_agent(ws_slug: str, agent_slug: str, display_name: str | None = None) -> None:
    ws = PROJECTS_ROOT / ws_slug
    (ws / "agents").mkdir(parents=True, exist_ok=True)
    (ws / "agents" / f"{agent_slug}.agent.md").write_text(
        f'---\nslug: "{agent_slug}"\ndisplay_name: "{display_name or agent_slug}"\n---\n\nBody\n',
        encoding="utf-8",
    )


async def test_list_without_project_is_global_only(client, make_project):
    project = await make_project("proj-g1")
    _write_project_agent("proj-g1", "proj-g1--local", "Local")

    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    slugs = [a["slug"] for a in resp.json()]
    assert "proj-g1--local" not in slugs


async def test_list_with_project_lazily_syncs_and_filters(client, make_project):
    project = await make_project("proj-g2")
    _write_project_agent("proj-g2", "proj-g2--local", "Local")

    resp = await client.get(f"/api/agents?project_id={project.project_id}")
    assert resp.status_code == 200
    agents = resp.json()
    slugs = [a["slug"] for a in agents]
    assert "proj-g2--local" in slugs
    local = next(a for a in agents if a["slug"] == "proj-g2--local")
    assert local["project_id"] == str(project.project_id)
    assert local["display_name"] == "Local"


async def test_list_with_project_excludes_other_projects_agents(client, make_project):
    project_a = await make_project("proj-g3")
    project_b = await make_project("proj-g4")
    _write_project_agent("proj-g3", "proj-g3--a", "A")
    _write_project_agent("proj-g4", "proj-g4--b", "B")

    resp = await client.get(f"/api/agents?project_id={project_a.project_id}")
    slugs = [a["slug"] for a in resp.json()]
    assert "proj-g3--a" in slugs
    assert "proj-g4--b" not in slugs


async def test_explicit_sync_endpoint(client, make_project):
    project = await make_project("proj-g5")
    _write_project_agent("proj-g5", "proj-g5--b", "B")

    resp = await client.post(f"/api/agents/sync?project_id={project.project_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "proj-g5--b" in body["synced"]

    # Removing the file then re-syncing reports it as removed.
    (PROJECTS_ROOT / "proj-g5" / "agents" / "proj-g5--b.agent.md").unlink()
    resp = await client.post(f"/api/agents/sync?project_id={project.project_id}")
    assert resp.status_code == 200
    assert "proj-g5--b" in resp.json()["removed"]


async def test_list_with_unknown_project_is_404(client):
    resp = await client.get("/api/agents?project_id=no-such-project")
    assert resp.status_code == 404
