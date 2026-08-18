"""Isolation: a project agent is only selectable in its own project."""
from __future__ import annotations

from sqlalchemy import select

from api.models.agent import Agent
from api.paths import PROJECTS_ROOT


def _write_project_agent(ws_slug: str, agent_slug: str) -> None:
    ws = PROJECTS_ROOT / ws_slug
    (ws / "agents").mkdir(parents=True, exist_ok=True)
    (ws / "agents" / f"{agent_slug}.agent.md").write_text(
        f'---\nslug: "{agent_slug}"\ndisplay_name: "{agent_slug}"\n---\n\nBody\n',
        encoding="utf-8",
    )


async def test_project_agent_only_valid_in_own_project(client, make_project):
    project_a = await make_project("proj-s1")
    project_b = await make_project("proj-s2")
    _write_project_agent("proj-s1", "proj-s1--helper")
    # Lazy sync registers the project agent for A.
    resp = await client.get(f"/api/agents?project_id={project_a.project_id}")
    assert resp.status_code == 200

    # Own project: OK.
    resp = await client.post(
        f"/api/projects/{project_a.project_id}/conversations",
        json={"agent_id": "proj-s1--helper"},
    )
    assert resp.status_code == 200

    # Other project: 404 (agent belongs to A only).
    resp = await client.post(
        f"/api/projects/{project_b.project_id}/conversations",
        json={"agent_id": "proj-s1--helper"},
    )
    assert resp.status_code == 404


async def test_global_agent_still_usable_in_any_project(client, db, make_project):
    project = await make_project("proj-s3")
    db.add(Agent(slug="gbl-test", display_name="Global Test", is_system=True))
    await db.commit()

    resp = await client.post(
        f"/api/projects/{project.project_id}/conversations",
        json={"agent_id": "gbl-test"},
    )
    assert resp.status_code == 200
