"""Router tests: project_id filtering, lazy sync, explicit sync endpoint."""
from __future__ import annotations

import pytest

from api.main import app
from api.paths import AGENTS_DIR, PROJECTS_ROOT, WORKFLOWS_DIR
from api.routers.agents import _parse_refs, _parse_str_list, _parse_tool_list
from api.services.agent_sync import sync_agents_dir

_MISSING = object()


@pytest.fixture
def skill_registry():
    """Install the real framework skill registry on the app.

    ASGITransport never runs the lifespan, so app.state.skill_registry is
    normally absent; restore the prior state afterwards (other session-scoped
    fixtures may have set it) to avoid leaking across tests.
    """
    from agent_core.skills.registry import SkillRegistry

    original = getattr(app.state, "skill_registry", _MISSING)
    registry = SkillRegistry()
    registry.load_dir(
        str(AGENTS_DIR / "skills"),
        mixin_dir=str(AGENTS_DIR / "skills" / "_mixins"),
    )
    app.state.skill_registry = registry
    yield registry
    if original is _MISSING:
        if hasattr(app.state, "skill_registry"):
            delattr(app.state, "skill_registry")
    else:
        app.state.skill_registry = original


@pytest.fixture
def no_skill_registry():
    original = getattr(app.state, "skill_registry", _MISSING)
    if original is not _MISSING:
        delattr(app.state, "skill_registry")
    yield
    if original is not _MISSING:
        app.state.skill_registry = original


@pytest.fixture
def workflow_registry():
    """Install the real framework workflow registry on the app (see
    skill_registry for why ASGITransport needs this by hand)."""
    from agent_core.skills.registry import SkillRegistry

    original = getattr(app.state, "workflow_registry", _MISSING)
    registry = SkillRegistry()
    registry.load_dir(str(WORKFLOWS_DIR))
    app.state.workflow_registry = registry
    yield registry
    if original is _MISSING:
        if hasattr(app.state, "workflow_registry"):
            delattr(app.state, "workflow_registry")
    else:
        app.state.workflow_registry = original


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


async def test_public_skill_injected_into_every_agent(client, db, skill_registry):
    # Global agent rows may not be synced yet (no lifespan in tests), so sync
    # the framework dir first — the assertion needs real agents to hold.
    await sync_agents_dir(db, AGENTS_DIR, project_id=None, is_system=True)

    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert agents  # framework agents synced
    for a in agents:
        km = [s for s in a["skills"] if s["slug"] == "knowledge/knowledge-manager"]
        assert len(km) == 1, f"{a['slug']}: {len(km)} knowledge-manager entries"
        assert km[0]["public"] is True
        # Every other skill stays non-public.
        assert all(
            not s["public"] for s in a["skills"] if s["slug"] != "knowledge/knowledge-manager"
        )


async def test_public_skill_dedupes_entry_still_listed_in_frontmatter(
    client, make_project, skill_registry
):
    # Project agent files are LLM-authored and may still list the slug — the
    # merge must flag that row public instead of appending a duplicate.
    project = await make_project("proj-pub")
    _write_project_agent(
        "proj-pub",
        "proj-pub--local",
        "Local",
        extra_frontmatter="skills:\n  - knowledge/knowledge-manager\n",
    )

    resp = await client.get(f"/api/agents?project_id={project.project_id}")
    assert resp.status_code == 200
    local = next(a for a in resp.json() if a["slug"] == "proj-pub--local")
    km = [s for s in local["skills"] if s["slug"] == "knowledge/knowledge-manager"]
    assert len(km) == 1
    assert km[0]["public"] is True


async def test_list_without_registry_still_returns_200(client, no_skill_registry):
    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    # No registry => no injection; agents (if any) carry only their own skills.
    assert all(
        not s.get("public")
        for a in resp.json()
        for s in a["skills"]
    )


async def test_locale_map_agent_returns_canonical_plus_i18n(client, make_project):
    """Bilingual frontmatter flows through sync into the response: the
    top-level fields stay zh canonical, i18n carries the locale maps."""
    project = await make_project("proj-i18nr")
    ws = PROJECTS_ROOT / "proj-i18nr"
    (ws / "agents").mkdir(parents=True, exist_ok=True)
    (ws / "agents" / "proj-i18nr--bi.agent.md").write_text(
        "---\n"
        'slug: "proj-i18nr--bi"\n'
        "display_name:\n  zh-CN: 助手\n  en-US: Helper\n"
        "description:\n  zh-CN: 帮你做事\n  en-US: Helps you out\n"
        "placeholder:\n  zh-CN: 说点什么…\n  en-US: Say something…\n"
        "starter_prompts:\n"
        "  zh-CN: [帮我做事]\n"
        "  en-US: [Do my work]\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )

    resp = await client.get(f"/api/agents?project_id={project.project_id}")
    assert resp.status_code == 200
    local = next(a for a in resp.json() if a["slug"] == "proj-i18nr--bi")
    assert local["display_name"] == "助手"
    assert local["description"] == "帮你做事"
    assert local["placeholder"] == "说点什么…"
    assert local["starter_prompts"] == ["帮我做事"]
    assert local["i18n"] == {
        "display_name": {"zh-CN": "助手", "en-US": "Helper"},
        "description": {"zh-CN": "帮你做事", "en-US": "Helps you out"},
        "placeholder": {"zh-CN": "说点什么…", "en-US": "Say something…"},
        "starter_prompts": {"zh-CN": ["帮我做事"], "en-US": ["Do my work"]},
    }


async def test_scalar_agent_has_null_i18n(client, make_project):
    project = await make_project("proj-ni18nr")
    _write_project_agent("proj-ni18nr", "proj-ni18nr--plain", "Plain")

    resp = await client.get(f"/api/agents?project_id={project.project_id}")
    local = next(a for a in resp.json() if a["slug"] == "proj-ni18nr--plain")
    assert local["i18n"] is None


async def test_non_public_skill_description_backfilled_from_registry(
    client, make_project, skill_registry
):
    """Frontmatter lists skills by slug — the tooltip description must come
    back from the registry even for non-public skills."""
    project = await make_project("proj-skb")
    _write_project_agent(
        "proj-skb",
        "proj-skb--local",
        "Local",
        extra_frontmatter="skills:\n  - integration/pr-review-manager\n",
    )

    resp = await client.get(f"/api/agents?project_id={project.project_id}")
    local = next(a for a in resp.json() if a["slug"] == "proj-skb--local")
    pr = [s for s in local["skills"] if s["slug"] == "integration/pr-review-manager"]
    assert len(pr) == 1
    assert pr[0]["description"]  # no longer empty
    assert pr[0]["public"] is False


async def test_workflow_description_backfilled_from_registry(
    client, make_project, skill_registry, workflow_registry
):
    """Workflows were never enriched at all — every tooltip description was
    empty. The registry now fills description + i18n (no public badge)."""
    project = await make_project("proj-wfb")
    _write_project_agent(
        "proj-wfb",
        "proj-wfb--local",
        "Local",
        extra_frontmatter="workflows:\n  - tech-lead-jira-implementation-to-pr\n",
    )

    resp = await client.get(f"/api/agents?project_id={project.project_id}")
    local = next(a for a in resp.json() if a["slug"] == "proj-wfb--local")
    wf = [w for w in local["workflows"] if w["slug"] == "tech-lead-jira-implementation-to-pr"]
    assert len(wf) == 1
    assert wf[0]["description"]


async def test_public_workflow_injected_into_every_agent(client, db, workflow_registry):
    """A `public: true` workflow appears on every agent's list even though no
    agent frontmatter declares it — same semantics as public skills."""
    public_slugs = {w.slug for w in workflow_registry.all() if w.public}
    assert public_slugs, "framework workflows should carry at least one public entry"

    await sync_agents_dir(db, AGENTS_DIR, project_id=None, is_system=True)

    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert agents  # framework agents synced
    for a in agents:
        for slug in public_slugs:
            matches = [w for w in a["workflows"] if w["slug"] == slug]
            assert len(matches) == 1, f"{a['slug']}: {len(matches)} {slug} entries"
            assert matches[0]["public"] is True


async def test_public_workflow_dedupes_entry_still_listed_in_frontmatter(
    client, make_project, workflow_registry
):
    """Project agent files may still list the public slug — the merge must
    flag that row public instead of appending a duplicate."""
    public_slug = next(w.slug for w in workflow_registry.all() if w.public)
    project = await make_project("proj-pubwf")
    _write_project_agent(
        "proj-pubwf",
        "proj-pubwf--local",
        "Local",
        extra_frontmatter=f"workflows:\n  - {public_slug}\n",
    )

    resp = await client.get(f"/api/agents?project_id={project.project_id}")
    assert resp.status_code == 200
    local = next(a for a in resp.json() if a["slug"] == "proj-pubwf--local")
    matches = [w for w in local["workflows"] if w["slug"] == public_slug]
    assert len(matches) == 1
    assert matches[0]["public"] is True


async def test_skill_registry_i18n_map_passthrough(client, make_project):
    """A registry skill with a locale-map description exposes its
    non-canonical variants via SkillRef.i18n."""
    from agent_core.skills.loader import SkillDefinition
    from agent_core.skills.registry import SkillRegistry

    original = getattr(app.state, "skill_registry", _MISSING)
    registry = SkillRegistry()
    registry._skills["testing/bilingual"] = SkillDefinition(
        slug="testing/bilingual",
        skill_type="guidance",
        description="Bilingual skill",
        description_i18n={"zh-CN": "双语技能"},
        body="",
    )
    app.state.skill_registry = registry
    try:
        project = await make_project("proj-ski18n")
        _write_project_agent(
            "proj-ski18n",
            "proj-ski18n--local",
            "Local",
            extra_frontmatter="skills:\n  - testing/bilingual\n",
        )

        resp = await client.get(f"/api/agents?project_id={project.project_id}")
        local = next(a for a in resp.json() if a["slug"] == "proj-ski18n--local")
        skill = next(s for s in local["skills"] if s["slug"] == "testing/bilingual")
        assert skill["description"] == "Bilingual skill"
        assert skill["i18n"] == {"zh-CN": "双语技能"}
    finally:
        if original is _MISSING:
            if hasattr(app.state, "skill_registry"):
                delattr(app.state, "skill_registry")
        else:
            app.state.skill_registry = original
