"""Router tests: project_id filtering, lazy sync, explicit sync endpoint."""
from __future__ import annotations

from api.paths import PROJECTS_ROOT
from api.routers.agents import _parse_refs, _parse_str_list, _parse_tool_list


def _write_project_agent(
    ws_slug: str,
    agent_slug: str,
    display_name: str | None = None,
    extra_frontmatter: str = "",
) -> None:
    ws = PROJECTS_ROOT / ws_slug
    (ws / "agents").mkdir(parents=True, exist_ok=True)
    (ws / "agents" / f"{agent_slug}.agent.md").write_text(
        f'---\nslug: "{agent_slug}"\ndisplay_name: "{display_name or agent_slug}"\n'
        f"{extra_frontmatter}---\n\nBody\n",
        encoding="utf-8",
    )


def test_parse_tool_list_handles_scalar_json_string():
    # agent_sync JSON-dumps a scalar `tools:` frontmatter value, so a list
    # like `tools: "shell, filesystem"` is stored as `"\"shell, filesystem\""`.
    assert _parse_tool_list('"shell, filesystem"') == ["shell", "filesystem"]


def test_parse_tool_list_handles_plain_list():
    assert _parse_tool_list('["shell", "filesystem"]') == ["shell", "filesystem"]


def test_parse_tool_list_handles_raw_non_json():
    assert _parse_tool_list("shell, filesystem") == ["shell", "filesystem"]


def test_parse_refs_handles_scalar_json_string():
    assert [r.slug for r in _parse_refs('"code-review"')] == ["code-review"]


def test_parse_refs_handles_scalar_comma_json_string():
    assert [r.slug for r in _parse_refs('"code-review, knowledge-manager"')] == [
        "code-review",
        "knowledge-manager",
    ]


def test_parse_refs_handles_list_of_dicts():
    refs = _parse_refs('[{"slug": "a", "description": "A"}, "b"]')
    assert [(r.slug, r.description) for r in refs] == [("a", "A"), ("b", "")]


def test_parse_str_list_handles_json_list():
    assert _parse_str_list('["a, b", "c"]') == ["a, b", "c"]


def test_parse_str_list_never_comma_splits():
    # Prompts are free text — a comma stays inside one item.
    assert _parse_str_list('"Review this PR for correctness, risk"') == [
        "Review this PR for correctness, risk"
    ]


def test_parse_str_list_handles_garbage():
    assert _parse_str_list("not json") == []
    assert _parse_str_list(None) == []
    assert _parse_str_list("") == []
    assert _parse_str_list('{"a": 1}') == []
    assert _parse_str_list('["ok", 42, null]') == ["ok"]


async def test_list_without_project_is_global_only(client, make_project):
    project = await make_project("proj-g1")
    _write_project_agent("proj-g1", "proj-g1--local", "Local")

    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    slugs = [a["slug"] for a in resp.json()]
    assert "proj-g1--local" not in slugs


async def test_list_with_project_lazily_syncs_and_filters(client, make_project):
    project = await make_project("proj-g2")
    _write_project_agent(
        "proj-g2",
        "proj-g2--local",
        "Local",
        extra_frontmatter=(
            'placeholder: "Describe what you need…"\n'
            "starter_prompts:\n"
            '  - "Do the thing, please"\n'
            '  - "Second prompt"\n'
        ),
    )

    resp = await client.get(f"/api/agents?project_id={project.project_id}")
    assert resp.status_code == 200
    agents = resp.json()
    slugs = [a["slug"] for a in agents]
    assert "proj-g2--local" in slugs
    local = next(a for a in agents if a["slug"] == "proj-g2--local")
    assert local["project_id"] == str(project.project_id)
    assert local["display_name"] == "Local"
    assert local["placeholder"] == "Describe what you need…"
    # The comma inside the first prompt must not split it.
    assert local["starter_prompts"] == ["Do the thing, please", "Second prompt"]


async def test_list_agent_without_guidance_fields_defaults(client, make_project):
    project = await make_project("proj-g6")
    _write_project_agent("proj-g6", "proj-g6--bare", "Bare")

    resp = await client.get(f"/api/agents?project_id={project.project_id}")
    assert resp.status_code == 200
    bare = next(a for a in resp.json() if a["slug"] == "proj-g6--bare")
    assert bare["placeholder"] is None
    assert bare["starter_prompts"] == []


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
