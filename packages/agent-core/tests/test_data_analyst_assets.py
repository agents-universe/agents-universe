"""Framework asset integrity tests for the data-analyst agent.

Guards the contract between markdown assets and the runtime:
- agent slug matches its filename and frontmatter,
- every tool listed in frontmatter resolves in the tool registry,
- every skill/workflow reference resolves in the loaded registries,
- skill slugs match their file paths, bilingual triggers are present,
- the analysis toolchain never touches ``sql_query`` (platform DB only),
- the dataviz reference in the analysis-patterns template is closed.
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
TEMPLATE_DIR = REPO_ROOT / "knowledge" / "_template"

ANALYSIS_SKILLS = [
    "analysis/sql-crafter",
    "analysis/local-file-analyst",
    "analysis/data-profiler",
    "analysis/metric-investigator",
    "analysis/dataviz",
    "analysis/report-writer",
]
DATA_WORKFLOWS = [
    "ad-hoc-analysis",
    "metric-deep-dive",
    "recurring-report",
    "data-source-onboarding",
]
KNOWN_REFERENCED_SKILLS = ANALYSIS_SKILLS + [
    "knowledge/knowledge-manager",
    "interaction/user-confirm",
    "office/xlsx",
]

_CJK_RE = re.compile(r"[一-鿿]")


def _valid_tools() -> set[str]:
    # memory_rw exposes ``name`` as a property, so read it off an instance.
    return {tool.name for cls in _CORE_TOOLS for tool in (cls(),)} | set(_OPTIONAL_TOOL_MODULES)


def _load_agent() -> AgentConfig:
    return AgentConfig.from_file(str(AGENTS_DIR / "data-analyst.agent.md"))


def _skills() -> dict[str, object]:
    return {s.slug: s for s in load_skills_from_dir(SKILLS_DIR)}


def _workflows() -> dict[str, object]:
    return {w.slug: w for w in load_skills_from_dir(WORKFLOWS_DIR)}


# ── Agent definition ────────────────────────────────────────────────────────


def test_agent_slug_matches_filename_and_frontmatter():
    cfg = _load_agent()
    assert cfg.slug == "data-analyst"
    assert (AGENTS_DIR / f"{cfg.slug}.agent.md").exists()
    assert cfg.description


def test_agent_tools_valid_and_exclude_sql_query():
    cfg = _load_agent()
    valid = _valid_tools()
    assert set(cfg.tools) <= valid, f"unknown tools: {set(cfg.tools) - valid}"
    assert "sql_query" not in cfg.tools  # platform app DB only — not for business data


def test_agent_skill_references_resolve():
    cfg = _load_agent()
    loaded = _skills()
    missing = [s for s in cfg.skills if s not in loaded]
    assert missing == []
    assert set(cfg.skills) == set(KNOWN_REFERENCED_SKILLS)


def test_agent_workflow_references_resolve():
    cfg = _load_agent()
    loaded = _workflows()
    missing = [w for w in cfg.workflows if w not in loaded]
    assert missing == []
    assert set(cfg.workflows) == set(DATA_WORKFLOWS)


def test_agent_body_references_skill_and_workflow_paths():
    cfg = _load_agent()
    for slug in KNOWN_REFERENCED_SKILLS:
        assert f"agents/skills/{slug}.md" in cfg.system_prompt
    for wf in DATA_WORKFLOWS:
        assert f"workflows/{wf}.workflow.md" in cfg.system_prompt


# ── Analysis skills ─────────────────────────────────────────────────────────


def test_analysis_skill_slug_matches_path_and_metadata():
    loaded = _skills()
    for slug in ANALYSIS_SKILLS:
        path = SKILLS_DIR / f"{slug}.md"
        assert path.exists(), f"missing skill file {path}"
        skill = loaded[slug]
        assert skill.slug == slug
        assert skill.description
        assert skill.triggers, f"{slug} has no triggers"
        assert skill.skill_type == "guidance"


def test_analysis_skill_triggers_are_bilingual():
    loaded = _skills()
    for slug in ANALYSIS_SKILLS:
        triggers = " ".join(loaded[slug].triggers)
        assert any(_CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing zh trigger"
        assert any(not _CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing en trigger"


def test_analysis_skill_tools_are_valid():
    loaded = _skills()
    valid = _valid_tools()
    for slug in ANALYSIS_SKILLS:
        bad = set(loaded[slug].tools) - valid
        assert not bad, f"{slug} references unknown tools: {bad}"


# ── Data workflows ──────────────────────────────────────────────────────────


def test_data_workflow_slug_matches_path_and_metadata():
    loaded = _workflows()
    for slug in DATA_WORKFLOWS:
        path = WORKFLOWS_DIR / f"{slug}.workflow.md"
        assert path.exists(), f"missing workflow file {path}"
        wf = loaded[slug]
        assert wf.slug == slug
        assert wf.description
        assert wf.triggers, f"{slug} has no triggers"


def test_data_workflow_triggers_are_bilingual():
    loaded = _workflows()
    for slug in DATA_WORKFLOWS:
        assert any(_CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing zh trigger"
        assert any(not _CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing en trigger"


def test_data_workflow_tools_are_valid():
    loaded = _workflows()
    valid = _valid_tools()
    for slug in DATA_WORKFLOWS:
        bad = set(loaded[slug].tools) - valid
        assert not bad, f"{slug} references unknown tools: {bad}"


def test_analysis_frontmatter_never_uses_sql_query():
    loaded = {**{s.slug: s for s in load_skills_from_dir(SKILLS_DIR)},
              **{w.slug: w for w in load_skills_from_dir(WORKFLOWS_DIR)}}
    for slug in ANALYSIS_SKILLS + DATA_WORKFLOWS:
        assert "sql_query" not in loaded[slug].tools, f"{slug} must not use sql_query"


# ── Trigger matching (workflow activation) ─────────────────────────────────


def test_chinese_triggers_activate_workflows():
    reg = SkillRegistry()
    reg.load_dir(WORKFLOWS_DIR)
    expectations = {
        "帮我分析一下销售数据": "ad-hoc-analysis",
        "指标为什么下降": "metric-deep-dive",
        "生成周报": "recurring-report",
        "接入数据源": "data-source-onboarding",
    }
    for msg, expected in expectations.items():
        slugs = [w.slug for w in reg.matching_triggers(msg)]
        assert expected in slugs, f"{msg!r} did not activate {expected}: {slugs}"


def test_english_triggers_activate_workflows():
    reg = SkillRegistry()
    reg.load_dir(WORKFLOWS_DIR)
    expectations = {
        "help me analyze this data": "ad-hoc-analysis",
        "the metric dropped yesterday": "metric-deep-dive",
        "weekly report please": "recurring-report",
        "onboard a data source": "data-source-onboarding",
    }
    for msg, expected in expectations.items():
        slugs = [w.slug for w in reg.matching_triggers(msg)]
        assert expected in slugs, f"{msg!r} did not activate {expected}: {slugs}"


# ── Knowledge template cross-reference closure ──────────────────────────────


def test_dataviz_reference_closed_in_analysis_patterns_template():
    template = TEMPLATE_DIR / "analysis-patterns.md"
    assert template.exists()
    text = template.read_text(encoding="utf-8")
    # The template defers styling to the global dataviz skill — the skill must
    # exist and the reference must name its real path.
    assert "dataviz" in text
    assert "agents/skills/analysis/dataviz.md" in text
    assert (SKILLS_DIR / "analysis" / "dataviz.md").exists()
