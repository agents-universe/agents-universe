"""Framework asset integrity tests for the orchestrator (超级智能体).

The orchestrator is the default conversation entry point: its routing table
is the only in-prompt map from task type to specialist, so every other shipped
agent slug must appear in the body — a new agent added without a routing row
would be invisible to the coordinator that is supposed to find it.
"""
from __future__ import annotations

from pathlib import Path

import frontmatter

from agent_core.agent import AgentConfig
from agent_core.skills.loader import load_skills_from_dir
from agent_core.tools.registry import _CORE_TOOLS, _OPTIONAL_TOOL_MODULES

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"
SKILLS_DIR = AGENTS_DIR / "skills"

SLUG = "orchestrator"


def _valid_tools() -> set[str]:
    # memory_rw exposes ``name`` as a property, so read it off an instance.
    return {tool.name for cls in _CORE_TOOLS for tool in (cls(),)} | set(_OPTIONAL_TOOL_MODULES)


def _load_agent() -> AgentConfig:
    return AgentConfig.from_file(str(AGENTS_DIR / f"{SLUG}.agent.md"))


def _raw_frontmatter() -> dict:
    return dict(frontmatter.load(str(AGENTS_DIR / f"{SLUG}.agent.md")).metadata)


# ── Agent definition ────────────────────────────────────────────────────────


def test_agent_slug_matches_filename_and_frontmatter():
    cfg = _load_agent()
    assert cfg.slug == SLUG
    assert (AGENTS_DIR / f"{cfg.slug}.agent.md").exists()
    assert cfg.description


def test_agent_tools_valid():
    cfg = _load_agent()
    valid = _valid_tools()
    assert set(cfg.tools) <= valid, f"unknown tools: {set(cfg.tools) - valid}"


def test_agent_declares_delegation_tools():
    """Without both tools the coordinator is structurally blind: list_agents
    discovers the roster, delegate_agent acts on it."""
    cfg = _load_agent()
    assert "delegate_agent" in cfg.tools
    assert "list_agents" in cfg.tools


def test_agent_skill_references_resolve():
    cfg = _load_agent()
    loaded = {s.slug for s in load_skills_from_dir(SKILLS_DIR)}
    missing = [s for s in cfg.skills if s not in loaded]
    assert missing == []


def test_bilingual_locale_maps_parse():
    meta = _raw_frontmatter()
    for field in ("display_name", "description", "placeholder"):
        value = meta[field]
        assert isinstance(value, dict), f"{field} must be a locale map"
        assert value.get("zh-CN"), f"{field} missing zh-CN"
        assert value.get("en-US"), f"{field} missing en-US"
    starters = meta["starter_prompts"]
    assert isinstance(starters, dict)
    assert starters.get("zh-CN") and starters.get("en-US")
    assert len(starters["zh-CN"]) <= 10 and len(starters["en-US"]) <= 10


def test_budget_fields():
    cfg = _load_agent()
    assert cfg.max_tokens == 128000
    assert cfg.token_budget == 100000


# ── Routing table honesty ───────────────────────────────────────────────────


def test_every_other_global_agent_slug_appears_in_body():
    """The routing table must name every shipped specialist. A new
    agents/*.agent.md added without a row here would be undiscoverable by the
    coordinator, which is exactly the failure this whole agent exists to fix."""
    cfg = _load_agent()
    others = sorted(
        p.name.removesuffix(".agent.md")
        for p in AGENTS_DIR.glob("*.agent.md")
        if p.name.removesuffix(".agent.md") != SLUG
    )
    assert others, "expected other shipped agents to exist"
    missing = [slug for slug in others if f"`{slug}`" not in cfg.system_prompt]
    assert missing == [], f"routing table missing agents: {missing}"


def test_body_states_self_first_and_final_answer_rules():
    """Two load-bearing coordination contracts: simple work stays local, and
    the orchestrator — not a delegate — writes the reply the user reads."""
    body = _load_agent().system_prompt
    assert "自己做" in body
    assert "最终" in body  # integrated final answer is the orchestrator's job
    assert "list_agents" in body
    assert "4000" in body  # summary cap the briefs must respect
