"""Unit tests for agent definition path resolution."""
from __future__ import annotations

import pytest

from agent_core import definition_check

from api.services import agent_sync
from api.services.agent_sync import resolve_agent_definition_path, validate_agent_slug


def test_project_workspace_shadows_global(tmp_path):
    proj = tmp_path / "ws"
    (proj / "agents").mkdir(parents=True)
    (proj / "agents" / "dual.agent.md").write_text("---\nslug: dual\n---\nP\n", encoding="utf-8")

    path = resolve_agent_definition_path("dual", str(proj))
    assert path == str((proj / "agents" / "dual.agent.md"))


def test_falls_back_to_global_framework_dir(tmp_path):
    proj = tmp_path / "ws"
    proj.mkdir()
    # "tech-lead" exists only in the global framework agents/ dir.
    path = resolve_agent_definition_path("tech-lead", str(proj))
    assert path is not None
    assert path.replace("\\", "/").endswith("agents/tech-lead.agent.md")


def test_missing_definition_returns_none(tmp_path):
    proj = tmp_path / "ws"
    proj.mkdir()
    assert resolve_agent_definition_path("no-such-agent-xyz", str(proj)) is None


@pytest.mark.parametrize("bad_slug", ["../evil", "a/b", "a b", ".hidden", "..", "agents/x"])
def test_unsafe_slug_rejected(bad_slug):
    with pytest.raises(ValueError):
        resolve_agent_definition_path(bad_slug, None)


def test_validate_agent_slug_allows_project_prefix():
    assert validate_agent_slug("proj-a--helper") is True
    assert validate_agent_slug("tech-lead") is True
    assert validate_agent_slug("x") is True
    assert validate_agent_slug("") is False
    assert validate_agent_slug("A--X") is False


def test_slug_rules_are_the_same_object_as_agent_core():
    """The sync gate and the filesystem tool's write-time check must agree.

    Guard by identity, not by value: a copied constant would pass an equality
    assertion today and drift apart on the next edit, which is exactly how a
    file gets written green and then silently skipped at registration.
    """
    assert agent_sync.validate_agent_slug is definition_check.validate_agent_slug
    assert agent_sync.PROJECT_SLUG_SEPARATOR is definition_check.PROJECT_SLUG_SEPARATOR
