"""Framework asset integrity tests for the recruiting-assistant agent.

Mirrors ``test_office_assets.py``: slug/tool/skill references resolve, the
workflow binds to the agent, bilingual triggers are present, delegation
discovery finds the agent for recruiting queries, and the toolchain carries
email + doc_reader but never ``sql_query``.
"""
from __future__ import annotations

import re
from pathlib import Path

from agent_core.agent import AgentConfig
from agent_core.delegation import list_delegation_candidates
from agent_core.skills.loader import load_skills_from_dir
from agent_core.skills.registry import SkillRegistry
from agent_core.tools.registry import _CORE_TOOLS, _OPTIONAL_TOOL_MODULES

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = REPO_ROOT / "agents"
SKILLS_DIR = AGENTS_DIR / "skills"
WORKFLOWS_DIR = REPO_ROOT / "workflows"

RECRUITING_SKILLS = [
    "integration/email-reader",
    "screening/email-intake",
    "screening/resume-screening",
    "screening/candidate-summary",
]
GUIDANCE_SKILLS = [s for s in RECRUITING_SKILLS if s != "screening/candidate-summary"]
RECRUITING_TRIGGER_CASES = [
    ("帮我筛选这批简历", "screening/resume-screening"),
    ("screen these resumes and shortlist", "screening/resume-screening"),
    ("查收简历邮件", "screening/email-intake"),
    ("check inbox for resumes", "screening/email-intake"),
    ("check inbox", "integration/email-reader"),
    ("读一下这封邮件", "integration/email-reader"),
    ("整理初筛报告", "screening/candidate-summary"),
]

_CJK_RE = re.compile(r"[一-鿿]")


def _valid_tools() -> set[str]:
    # memory_rw exposes ``name`` as a property, so read it off an instance.
    return {tool.name for cls in _CORE_TOOLS for tool in (cls(),)} | set(_OPTIONAL_TOOL_MODULES)


def _load_agent() -> AgentConfig:
    return AgentConfig.from_file(str(AGENTS_DIR / "recruiting-assistant.agent.md"))


def _skills() -> dict[str, object]:
    return {s.slug: s for s in load_skills_from_dir(SKILLS_DIR)}


# ── Agent definition ────────────────────────────────────────────────────────


def test_agent_slug_matches_filename_and_frontmatter():
    cfg = _load_agent()
    assert cfg.slug == "recruiting-assistant"
    assert (AGENTS_DIR / f"{cfg.slug}.agent.md").exists()
    assert cfg.description


def test_agent_tools_valid_email_toolchain_and_no_sql_query():
    cfg = _load_agent()
    valid = _valid_tools()
    assert set(cfg.tools) <= valid, f"unknown tools: {set(cfg.tools) - valid}"
    # The intake→parse→deliver chain must be fully declared.
    assert {"email", "doc_reader", "filesystem", "deliver_file"} <= set(cfg.tools)
    assert "sql_query" not in cfg.tools  # platform app DB only — candidate data is user content
    assert "shell" not in cfg.tools


def test_agent_skill_references_resolve():
    cfg = _load_agent()
    loaded = _skills()
    missing = [s for s in cfg.skills if s not in loaded]
    assert missing == []
    assert set(RECRUITING_SKILLS) <= set(cfg.skills)


def test_agent_workflow_resolves_and_binds_to_agent():
    cfg = _load_agent()
    assert "resume-initial-screening" in cfg.workflows
    wf = WORKFLOWS_DIR / "resume-initial-screening.workflow.md"
    assert wf.exists()
    text = wf.read_text(encoding="utf-8")
    assert 'agent: "recruiting-assistant"' in text


def test_agent_body_references_skill_paths():
    cfg = _load_agent()
    for slug in RECRUITING_SKILLS:
        assert f"agents/skills/{slug}.md" in cfg.system_prompt


def test_agent_body_references_workflow_path():
    cfg = _load_agent()
    assert "workflows/resume-initial-screening.workflow.md" in cfg.system_prompt


def test_recruiting_delegation_discoverable_by_screening_queries():
    # Mirror ``ListAgentsTool``'s exact pipeline: roster from the definition
    # files, then case-insensitive substring over slug/display_name/description/
    # skills — other agents must find recruiting-assistant for resume work.
    roster = list_delegation_candidates(None, str(REPO_ROOT))
    records = [c.as_dict() for c in roster]
    assert any(r["slug"] == "recruiting-assistant" for r in records)
    for q in ("简历", "resume", "招聘", "screening", "shortlist"):
        hits = [
            r["slug"] for r in records
            if q in " ".join(
                [r["slug"], r["display_name"], r["description"], *r["skills"]]
            ).lower()
        ]
        assert "recruiting-assistant" in hits, f"{q!r} cannot find recruiting-assistant via list_agents"


# ── Recruiting skills ───────────────────────────────────────────────────────


def test_recruiting_skill_slug_matches_path_and_metadata():
    loaded = _skills()
    for slug in RECRUITING_SKILLS:
        path = SKILLS_DIR / f"{slug}.md"
        assert path.exists(), f"missing skill file {path}"
        skill = loaded[slug]
        assert skill.slug == slug
        assert skill.description
        assert skill.triggers, f"{slug} has no triggers"


def test_guidance_skills_have_guidance_type_and_candidate_summary_is_template():
    loaded = _skills()
    for slug in GUIDANCE_SKILLS:
        assert loaded[slug].skill_type == "guidance", f"{slug} should be guidance"
    assert loaded["screening/candidate-summary"].skill_type == "template"


def test_recruiting_skill_triggers_are_bilingual():
    loaded = _skills()
    for slug in RECRUITING_SKILLS:
        assert any(_CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing zh trigger"
        assert any(not _CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing en trigger"


def test_recruiting_skill_tools_are_valid():
    loaded = _skills()
    valid = _valid_tools()
    for slug in RECRUITING_SKILLS:
        bad = set(loaded[slug].tools) - valid
        assert not bad, f"{slug} references unknown tools: {bad}"


def test_recruiting_skills_never_use_sql_query_or_shell():
    loaded = _skills()
    for slug in RECRUITING_SKILLS:
        assert "sql_query" not in loaded[slug].tools, f"{slug} must not use sql_query"
        assert "shell" not in loaded[slug].tools, f"{slug} must not use shell"


def test_email_reader_relay_not_ask_password_contract():
    # The core "no email integration → guide, never ask password" contract
    # lives in the skill text; guard the phrases the agents are told to relay.
    text = (SKILLS_DIR / "integration/email-reader.md").read_text(encoding="utf-8")
    assert "Settings → 外部集成 → 邮件服务器" in text
    assert "Never ask the user to type a password into the chat" in text


# ── Trigger matching (skill activation) ────────────────────────────────────


def test_recruiting_triggers_activate_skills():
    reg = SkillRegistry()
    reg.load_dir(SKILLS_DIR)
    for msg, expected in RECRUITING_TRIGGER_CASES:
        slugs = [s.slug for s in reg.matching_triggers(msg)]
        assert expected in slugs, f"{msg!r} did not activate {expected}: {slugs}"
