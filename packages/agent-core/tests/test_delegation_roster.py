"""Delegation roster: which agents a project may hand a subtask to.

The roster decides what a delegating agent can even see, so it doubles as the
project-isolation boundary (CLAUDE.md #6): only this project's ``agents/`` dir
and the framework-wide one are scanned, and every advertised slug must resolve
back to the file it was read from — otherwise the model picks an agent whose
turn then fails to load.
"""
from __future__ import annotations

from pathlib import Path

from agent_core.delegation import (
    list_delegation_candidates,
    read_delegation_policy,
)

GLOBAL_AGENT = """---
slug: "{slug}"
display_name: "{name}"
description: "{desc}"
category: "{category}"
skills:
  - "generation/demo-maker"
workflows:
  - "demo-generation"
---

Body.
"""


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _agent(
    dir_path: Path,
    slug: str,
    *,
    stem: str | None = None,
    name: str | None = None,
    desc: str = "does things",
    category: str = "agile-development",
    frontmatter: str | None = None,
) -> Path:
    filename = stem if stem is not None else slug
    body = frontmatter if frontmatter is not None else GLOBAL_AGENT.format(
        slug=slug, name=name or slug, desc=desc, category=category
    )
    return _write(dir_path / f"{filename}.agent.md", body)


def _framework(tmp_path: Path) -> Path:
    root = tmp_path / "framework"
    (root / "agents").mkdir(parents=True, exist_ok=True)
    return root


def _workspace(tmp_path: Path, slug: str = "proj-a") -> Path:
    ws = tmp_path / slug
    (ws / "agents").mkdir(parents=True, exist_ok=True)
    return ws


def _resolve(slug: str, project_fs_path: Path | None, framework_root: Path) -> str | None:
    """Mirror agent_sync.resolve_agent_definition_path (project shadows global)."""
    if project_fs_path:
        project_path = project_fs_path / "agents" / f"{slug}.agent.md"
        if project_path.is_file():
            return str(project_path)
    global_path = framework_root / "agents" / f"{slug}.agent.md"
    return str(global_path) if global_path.is_file() else None


# --- discovery ---------------------------------------------------------------


def test_lists_project_and_framework_agents(tmp_path):
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "tech-lead", name="Tech Lead", category="agile-development")
    _agent(ws / "agents", "proj-a--helper", name="Helper", category="platform-assistant")

    candidates = list_delegation_candidates(str(ws), str(framework))

    assert [c.slug for c in candidates] == ["tech-lead", "proj-a--helper"]
    helper = candidates[1]
    assert helper.display_name == "Helper"
    assert helper.category == "platform-assistant"
    assert helper.skills == ("generation/demo-maker",)
    assert helper.workflows == ("demo-generation",)


def test_project_definition_shadows_framework_with_same_slug(tmp_path):
    """Same rule as the runtime resolution: the project copy wins."""
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "proj-a--helper", desc="framework copy")
    _agent(ws / "agents", "proj-a--helper", desc="project copy")

    candidates = list_delegation_candidates(str(ws), str(framework))

    assert len(candidates) == 1
    assert candidates[0].description == "project copy"
    assert candidates[0].definition_path == str(ws / "agents" / "proj-a--helper.agent.md")


def test_project_agent_without_project_prefix_is_excluded(tmp_path):
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(ws / "agents", "helper")

    assert list_delegation_candidates(str(ws), str(framework)) == []


def test_agent_borrowing_another_projects_prefix_is_excluded(tmp_path):
    """Cross-project leakage: a definition copied from proj-b must not appear."""
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path, "proj-a")
    _agent(ws / "agents", "proj-b--helper")

    assert list_delegation_candidates(str(ws), str(framework)) == []


def test_sibling_project_agents_are_not_visible(tmp_path):
    framework = _framework(tmp_path)
    ws_a = _workspace(tmp_path, "proj-a")
    ws_b = _workspace(tmp_path, "proj-b")
    _agent(ws_b / "agents", "proj-b--secret")

    assert list_delegation_candidates(str(ws_a), str(framework)) == []


def test_slug_not_matching_filename_is_excluded(tmp_path):
    """The runtime loads `{slug}.agent.md`; a mismatched stem cannot load."""
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "tech-lead", stem="other-name")

    assert list_delegation_candidates(str(ws), str(framework)) == []


def test_missing_or_unsafe_slug_is_excluded(tmp_path):
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "no-slug", frontmatter="---\ndisplay_name: x\n---\n\nBody.\n")
    _agent(
        framework / "agents", "unsafe",
        frontmatter='---\nslug: "../escape"\ndisplay_name: x\n---\n\nBody.\n',
    )

    assert list_delegation_candidates(str(ws), str(framework)) == []


def test_unparsable_frontmatter_is_excluded(tmp_path):
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "broken", frontmatter="---\nslug: [unclosed\n---\n\nBody.\n")

    assert list_delegation_candidates(str(ws), str(framework)) == []


def test_scalar_list_fields_are_normalized(tmp_path):
    """LLM-written definitions spell lists as comma-separated scalars."""
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(
        framework / "agents", "tech-lead",
        frontmatter=(
            "---\nslug: tech-lead\ndisplay_name: Tech Lead\n"
            'skills: "a, b"\nworkflows: "demo-generation"\n---\n\nBody.\n'
        ),
    )

    candidate = list_delegation_candidates(str(ws), str(framework))[0]

    assert candidate.skills == ("a", "b")
    assert candidate.workflows == ("demo-generation",)


# --- filtering ---------------------------------------------------------------


def test_exclude_drops_self_and_running_chain(tmp_path):
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "tech-lead")
    _agent(framework / "agents", "quality-assurance")
    _agent(framework / "agents", "project-owner")

    candidates = list_delegation_candidates(
        str(ws), str(framework), exclude=["tech-lead", "quality-assurance"]
    )

    assert [c.slug for c in candidates] == ["project-owner"]


def test_delegates_to_allowlist_restricts_targets(tmp_path):
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "tech-lead")
    _agent(framework / "agents", "office-assistant")

    candidates = list_delegation_candidates(
        str(ws), str(framework), allow=["tech-lead"]
    )

    assert [c.slug for c in candidates] == ["tech-lead"]


def test_empty_allowlist_means_no_restriction(tmp_path):
    """An unset/empty `delegates_to` must not read as "forbid everything"."""
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "tech-lead")

    assert len(list_delegation_candidates(str(ws), str(framework), allow=[])) == 1
    assert len(list_delegation_candidates(str(ws), str(framework), allow=None)) == 1


def test_missing_framework_root_lists_project_agents_only(tmp_path):
    ws = _workspace(tmp_path)
    _agent(ws / "agents", "proj-a--helper")

    candidates = list_delegation_candidates(str(ws), None)

    assert [c.slug for c in candidates] == ["proj-a--helper"]


def test_missing_dirs_yield_empty_roster(tmp_path):
    assert list_delegation_candidates(str(tmp_path / "nope"), str(tmp_path / "nope")) == []


# --- the resolution guarantee ------------------------------------------------


def test_every_advertised_slug_resolves_to_the_file_it_came_from(tmp_path):
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "tech-lead")
    _agent(framework / "agents", "quality-assurance")
    _agent(ws / "agents", "proj-a--helper")

    candidates = list_delegation_candidates(str(ws), str(framework))

    assert len(candidates) == 3
    for candidate in candidates:
        assert _resolve(candidate.slug, ws, framework) == candidate.definition_path


def test_ordering_is_stable_by_category_then_name(tmp_path):
    framework = _framework(tmp_path)
    ws = _workspace(tmp_path)
    _agent(framework / "agents", "zeta", name="Zeta", category="security")
    _agent(framework / "agents", "alpha", name="Alpha", category="security")
    _agent(framework / "agents", "beta", name="Beta", category="data-analysis")

    slugs = [c.slug for c in list_delegation_candidates(str(ws), str(framework))]

    assert slugs == ["beta", "alpha", "zeta"]


# --- per-agent policy --------------------------------------------------------


def test_read_delegation_policy_parses_list_and_scalar(tmp_path):
    listed = _agent(
        tmp_path / "a", "one",
        frontmatter='---\nslug: one\ndelegates_to:\n  - "tech-lead"\n  - "quality-assurance"\n---\n\nB.\n',
    )
    scalar = _agent(
        tmp_path / "b", "two",
        frontmatter='---\nslug: two\ndelegates_to: "tech-lead"\n---\n\nB.\n',
    )
    none = _agent(tmp_path / "c", "three")

    assert read_delegation_policy(str(listed)) == ("tech-lead", "quality-assurance")
    assert read_delegation_policy(str(scalar)) == ("tech-lead",)
    assert read_delegation_policy(str(none)) == ()


def test_read_delegation_policy_tolerates_missing_and_broken_files(tmp_path):
    broken = _write(tmp_path / "broken.agent.md", "---\nslug: [oops\n---\n\nB.\n")

    assert read_delegation_policy(None) == ()
    assert read_delegation_policy(str(tmp_path / "absent.agent.md")) == ()
    assert read_delegation_policy(str(broken)) == ()
