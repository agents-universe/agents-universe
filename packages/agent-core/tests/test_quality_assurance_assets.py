"""Framework asset integrity tests for the quality-assurance agent.

Guards the contract between markdown assets and the runtime:
- agent slug matches its filename and frontmatter,
- every tool listed in frontmatter resolves in the tool registry,
- every skill/workflow reference resolves in the loaded registries,
- skill slugs match their file paths, bilingual triggers are present,
- the data-setup skill activates from a Chinese "create test data" request
  (the trigger path that keeps the recipe loop in context),
- the agent body points at the real knowledge slugs (no bare *.md names).
"""
from __future__ import annotations

import re
from pathlib import Path

from agent_core.agent import AgentConfig
from agent_core.skills.loader import load_skills_from_dir
from agent_core.skills.registry import SkillRegistry
from agent_core.tools.registry import _CORE_TOOLS, _OPTIONAL_TOOL_MODULES

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"
SKILLS_DIR = AGENTS_DIR / "skills"
WORKFLOWS_DIR = REPO_ROOT / "workflows"

# Skills whose frontmatter triggers were added so their bodies inject on
# data-setup / writeback / test-design requests.
TRIGGERED_SKILLS = [
    "testing/test-data-setup",
    "testing/test-designer",
    "knowledge/knowledge-manager",
    "integration/kong-reader",
    "testing/system-test-planner",
    "testing/release-regression-manager",
]

_CJK_RE = re.compile(r"[一-鿿]")


def _valid_tools() -> set[str]:
    # memory_rw exposes ``name`` as a property, so read it off an instance.
    return {tool.name for cls in _CORE_TOOLS for tool in (cls(),)} | set(_OPTIONAL_TOOL_MODULES)


def _load_agent() -> AgentConfig:
    return AgentConfig.from_file(str(AGENTS_DIR / "quality-assurance.agent.md"))


def _skills() -> dict[str, object]:
    return {s.slug: s for s in load_skills_from_dir(SKILLS_DIR)}


def _workflows() -> dict[str, object]:
    return {w.slug: w for w in load_skills_from_dir(WORKFLOWS_DIR)}


# ── Agent definition ────────────────────────────────────────────────────────


def test_agent_slug_matches_filename_and_frontmatter():
    cfg = _load_agent()
    assert cfg.slug == "quality-assurance"
    assert (AGENTS_DIR / f"{cfg.slug}.agent.md").exists()
    assert cfg.description


def test_agent_tools_valid():
    cfg = _load_agent()
    valid = _valid_tools()
    assert set(cfg.tools) <= valid, f"unknown tools: {set(cfg.tools) - valid}"


def test_agent_skill_references_resolve():
    cfg = _load_agent()
    loaded = _skills()
    missing = [s for s in cfg.skills if s not in loaded]
    assert missing == []


def test_agent_includes_test_data_setup_skill():
    """The recipe loop (lookup → one probe per channel → immediate writeback)
    lives in the test-data-setup skill body — without this entry the agent
    has no reference to it at all."""
    cfg = _load_agent()
    assert "testing/test-data-setup" in cfg.skills


def test_agent_workflow_references_resolve():
    cfg = _load_agent()
    loaded = _workflows()
    missing = [w for w in cfg.workflows if w not in loaded]
    assert missing == []


def test_agent_body_references_core_skill_paths():
    cfg = _load_agent()
    # Core skills the body must point at by path (the knowledge-write and
    # data-setup flows). Not every frontmatter skill needs a body mention —
    # some are referenced only through workflows.
    for slug in TRIGGERED_SKILLS:
        assert f"agents/skills/{slug}.md" in cfg.system_prompt, (
            f"body never points at {slug}"
        )


# ── Triggered skills ────────────────────────────────────────────────────────


def test_triggered_skills_exist_with_bilingual_triggers():
    loaded = _skills()
    for slug in TRIGGERED_SKILLS:
        path = SKILLS_DIR / f"{slug}.md"
        assert path.exists(), f"missing skill file {path}"
        skill = loaded[slug]
        assert skill.slug == slug
        assert skill.description
        assert skill.triggers, f"{slug} has no triggers"
        assert any(_CJK_RE.search(t) for t in skill.triggers), f"{slug} missing zh trigger"
        assert any(not _CJK_RE.search(t) for t in skill.triggers), f"{slug} missing en trigger"


def test_triggered_skills_tools_are_valid():
    loaded = _skills()
    valid = _valid_tools()
    for slug in TRIGGERED_SKILLS:
        bad = set(loaded[slug].tools) - valid
        assert not bad, f"{slug} references unknown tools: {bad}"


# ── Trigger matching (the symptom-2 regression) ─────────────────────────────


def _skill_registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.load_dir(SKILLS_DIR)
    return reg


def test_chinese_data_setup_request_activates_test_data_setup():
    reg = _skill_registry()
    slugs = [s.slug for s in reg.matching_triggers("帮我造测试数据")]
    assert "testing/test-data-setup" in slugs


def test_english_data_setup_request_activates_test_data_setup():
    reg = _skill_registry()
    slugs = [s.slug for s in reg.matching_triggers("create test data for orders")]
    assert "testing/test-data-setup" in slugs


def test_writeback_request_activates_knowledge_manager():
    reg = _skill_registry()
    slugs = [s.slug for s in reg.matching_triggers("把这次学到的写回知识")]
    assert "knowledge/knowledge-manager" in slugs


def test_test_design_request_activates_test_designer():
    reg = _skill_registry()
    slugs = [s.slug for s in reg.matching_triggers("为这个需求设计测试用例")]
    assert "testing/test-designer" in slugs


def test_matching_triggers_capped_at_skill_level():
    """Trigger matching itself is uncapped; run() slices to 3. The test here
    guards that a data-setup message does not also match dozens of skills via
    overly-broad triggers (bare words like '数据' / 'API')."""
    reg = _skill_registry()
    matched = reg.matching_triggers("帮我造测试数据")
    # Allow a small neighborhood (e.g. knowledge-manager if it shares a
    # phrase) but nothing like half the skill set.
    assert len(matched) <= 5, [s.slug for s in matched]


# ── Knowledge slug naming in the agent body ─────────────────────────────────


def test_body_uses_full_knowledge_slugs():
    """The Knowledge Structure tree and examples must use real slugs
    (technical/api-map, skills/test-data-setup) — bare 'api-map.md' made
    read/write miss and created parallel files."""
    cfg = _load_agent()
    body = cfg.system_prompt
    assert "technical/api-map" in body
    assert "skills/test-data-setup" in body
    assert "system/history" in body
    # No root-level legacy names left in tree/examples.
    assert "`api-map.md`" not in body
    assert "`test-data-setup.md`" not in body
    assert "`history.md`" not in body
    assert "`kong-map.md`" not in body


def test_body_documents_load_vs_read_and_writeback():
    cfg = _load_agent()
    body = cfg.system_prompt
    # Cross-turn load vs one-off read guidance (Knowledge-First #2).
    assert 'knowledge_rw(operation="load"' in body
    assert 'knowledge_rw(operation="read"' in body
    # Success-path API writeback hook (was 404/405/400 only).
    assert "API Knowledge Writeback" in body
    assert "technical/api/{service-slug}" in body
