"""Framework asset integrity tests for the office-assistant agent and office skills.

Mirrors ``test_data_analyst_assets.py``: guards the contract between the markdown
assets and the runtime — slug/tool/skill references resolve, bilingual triggers
are present, and the office toolchain never touches ``sql_query``.
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

OFFICE_SKILLS = [
    "office/pptx",
    "office/xlsx",
    "office/docx",
    "office/pdf",
    "office/web-slides",
]
OFFICE_TRIGGER_CASES = [
    ("帮我生成ppt", "office/pptx"),
    ("做一个网页版ppt", "office/web-slides"),
    ("把这个表导出成excel文件", "office/xlsx"),
    ("帮我写一份word文档", "office/docx"),
    ("把这周的分析报告导出成pdf", "office/pdf"),
]

_CJK_RE = re.compile(r"[一-鿿]")


def _valid_tools() -> set[str]:
    # memory_rw exposes ``name`` as a property, so read it off an instance.
    return {tool.name for cls in _CORE_TOOLS for tool in (cls(),)} | set(_OPTIONAL_TOOL_MODULES)


def _load_agent() -> AgentConfig:
    return AgentConfig.from_file(str(AGENTS_DIR / "office-assistant.agent.md"))


def _skills() -> dict[str, object]:
    return {s.slug: s for s in load_skills_from_dir(SKILLS_DIR)}


# ── Agent definition ────────────────────────────────────────────────────────


def test_agent_slug_matches_filename_and_frontmatter():
    cfg = _load_agent()
    assert cfg.slug == "office-assistant"
    assert (AGENTS_DIR / f"{cfg.slug}.agent.md").exists()
    assert cfg.description


def test_agent_tools_valid_and_exclude_sql_query():
    cfg = _load_agent()
    valid = _valid_tools()
    assert set(cfg.tools) <= valid, f"unknown tools: {set(cfg.tools) - valid}"
    assert "sql_query" not in cfg.tools  # platform app DB only — office data is user content


def test_agent_skill_references_resolve():
    cfg = _load_agent()
    loaded = _skills()
    missing = [s for s in cfg.skills if s not in loaded]
    assert missing == []
    assert set(cfg.skills) == set(OFFICE_SKILLS)


def test_agent_body_references_skill_paths():
    cfg = _load_agent()
    for slug in OFFICE_SKILLS:
        assert f"agents/skills/{slug}.md" in cfg.system_prompt


# ── Office skills ───────────────────────────────────────────────────────────


def test_office_skill_slug_matches_path_and_metadata():
    loaded = _skills()
    for slug in OFFICE_SKILLS:
        path = SKILLS_DIR / f"{slug}.md"
        assert path.exists(), f"missing skill file {path}"
        skill = loaded[slug]
        assert skill.slug == slug
        assert skill.description
        assert skill.triggers, f"{slug} has no triggers"
        assert skill.skill_type == "guidance"


def test_office_skill_triggers_are_bilingual():
    loaded = _skills()
    for slug in OFFICE_SKILLS:
        assert any(_CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing zh trigger"
        assert any(not _CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing en trigger"


def test_office_skill_tools_are_valid():
    loaded = _skills()
    valid = _valid_tools()
    for slug in OFFICE_SKILLS:
        bad = set(loaded[slug].tools) - valid
        assert not bad, f"{slug} references unknown tools: {bad}"


def test_office_skills_never_use_sql_query():
    loaded = _skills()
    for slug in OFFICE_SKILLS:
        assert "sql_query" not in loaded[slug].tools, f"{slug} must not use sql_query"


# ── Trigger matching (skill activation) ────────────────────────────────────


def test_office_triggers_activate_skills():
    reg = SkillRegistry()
    reg.load_dir(SKILLS_DIR)
    for msg, expected in OFFICE_TRIGGER_CASES:
        slugs = [s.slug for s in reg.matching_triggers(msg)]
        assert expected in slugs, f"{msg!r} did not activate {expected}: {slugs}"
