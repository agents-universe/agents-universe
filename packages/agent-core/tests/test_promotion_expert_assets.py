"""Framework asset integrity tests for the promotion-expert agent.

Mirrors ``test_performance_test_expert_assets.py``: guards the contract
between the markdown assets and the runtime - slug/tool/skill references
resolve, bilingual triggers are present, and the campaign gate model holds:
one ``promotion_campaign_approval`` replaces per-request dialogs (hence
``api_request_no_confirm is True`` - the deliberate inverse of the perf and
pentest agents, whose outbound traffic is probes rather than deliverable),
while read-back verification and the scheduled-run recheck stay encoded.
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

MARKETING_SKILLS = [
    "marketing/channel-strategy",
    "marketing/copy-adaptation",
    "marketing/campaign-launch",
    "marketing/content-calendar",
    "marketing/campaign-report",
    "marketing/market-reference",
]
KNOWN_REFERENCED_SKILLS = MARKETING_SKILLS + [
    "integration/github-pages-publisher",
    "integration/custom-api-onboarding",
    "analysis/report-writer",
    "generation/scheduled-task-writer",
    "interaction/user-confirm",
    "interaction/web-search-privacy",
]
KNOWN_REFERENCED_WORKFLOWS = [
    "promotion-engagement",
    "knowledge-ingestion",
]
# Injected into every agent at response time via `public: true` in the skill
# file - never listed in this agent's frontmatter `skills:`.
PUBLIC_SKILLS = {"knowledge/knowledge-manager"}

# Every channel of the publish loop must be backed by a declared tool.
CHANNEL_TOOLS = {
    "api_request",
    "email",
    "git_repo",
    "github",
    "browser_playwright",
    "scheduler",
    "skill_source",
    "user_confirm",
    "secret_vault",
    "web_search",
}

_CJK_RE = re.compile(r"[一-鿿]")


def _valid_tools() -> set[str]:
    # memory_rw exposes ``name`` as a property, so read it off an instance.
    return {tool.name for cls in _CORE_TOOLS for tool in (cls(),)} | set(_OPTIONAL_TOOL_MODULES)


def _load_agent() -> AgentConfig:
    return AgentConfig.from_file(str(AGENTS_DIR / "promotion-expert.agent.md"))


def _skills() -> dict[str, object]:
    return {s.slug: s for s in load_skills_from_dir(SKILLS_DIR)}


def _workflows() -> dict[str, object]:
    return {w.slug: w for w in load_skills_from_dir(WORKFLOWS_DIR)}


def _skill_text(slug: str) -> str:
    return (SKILLS_DIR / f"{slug}.md").read_text(encoding="utf-8")


def _workflow_text(slug: str) -> str:
    return (WORKFLOWS_DIR / f"{slug}.workflow.md").read_text(encoding="utf-8")


# ── Agent definition ────────────────────────────────────────────────────────


def test_agent_slug_matches_filename_and_frontmatter():
    cfg = _load_agent()
    assert cfg.slug == "promotion-expert"
    assert (AGENTS_DIR / f"{cfg.slug}.agent.md").exists()
    assert cfg.description


def test_agent_tools_valid_and_cover_every_channel():
    cfg = _load_agent()
    valid = _valid_tools()
    assert set(cfg.tools) <= valid, f"unknown tools: {set(cfg.tools) - valid}"
    missing = CHANNEL_TOOLS - set(cfg.tools)
    assert not missing, f"missing channel tools: {missing}"
    # sql_query reaches the platform's own DB (users/sessions/secrets) - never
    # a promotion instrument; arbitrary shell is likewise out of scope.
    assert "sql_query" not in cfg.tools
    assert "shell" not in cfg.tools
    assert "code_executor" not in cfg.tools
    assert "test_generator" not in cfg.tools
    # MCP channels are project opt-in (declared via the customization expert),
    # never shipped in the global definition.
    assert not any(t == "mcp" or t.startswith("mcp:") for t in cfg.tools)


def test_agent_campaign_gate_replaces_per_request_confirm():
    """Unlike perf/pentest (whose outbound traffic is probes and keeps the
    per-request gate), promotion's outbound posts ARE the deliverable: the
    single campaign approval replaces per-request dialogs by design."""
    cfg = _load_agent()
    assert cfg.api_request_no_confirm is True
    prompt = cfg.system_prompt
    # The gate model must be spelled out, not just flagged.
    assert "api_request_no_confirm" in prompt
    assert "promotion_campaign_approval" in prompt
    # Tool-internal gates survive and are stated as not bypassed.
    assert "NOT bypassed" in prompt
    assert "per send" in prompt
    assert "prd" in prompt.lower() or "production" in prompt.lower()


def test_agent_skill_references_resolve():
    cfg = _load_agent()
    loaded = _skills()
    missing = [s for s in cfg.skills if s not in loaded]
    assert missing == []
    assert set(cfg.skills) == set(KNOWN_REFERENCED_SKILLS) - PUBLIC_SKILLS
    assert not PUBLIC_SKILLS & set(cfg.skills)


def test_agent_workflow_references_resolve():
    cfg = _load_agent()
    loaded = _workflows()
    missing = [w for w in cfg.workflows if w not in loaded]
    assert missing == []
    assert set(cfg.workflows) == set(KNOWN_REFERENCED_WORKFLOWS)


def test_agent_body_references_skill_and_workflow_paths():
    cfg = _load_agent()
    for slug in KNOWN_REFERENCED_SKILLS:
        assert f"agents/skills/{slug}.md" in cfg.system_prompt, f"body missing skill path {slug}"
    for wf in KNOWN_REFERENCED_WORKFLOWS:
        assert f"workflows/{wf}.workflow.md" in cfg.system_prompt, f"body missing workflow path {wf}"


def test_agent_prompt_instructs_plan_task_for_workflow_phases():
    cfg = _load_agent()
    assert "call `plan_task`" in cfg.system_prompt
    assert "explicit task plan" in cfg.system_prompt
    # The Workflow section must reference the workflow the plan is drawn from.
    wf_idx = cfg.system_prompt.index("promotion-engagement")
    assert "plan_task" in cfg.system_prompt[wf_idx:]


def test_agent_locale_maps_and_starter_prompts():
    import frontmatter

    meta = dict(frontmatter.load(str(AGENTS_DIR / "promotion-expert.agent.md")).metadata)
    for field in ("display_name", "description", "placeholder"):
        value = meta.get(field)
        assert isinstance(value, dict), f"{field} must be a locale map"
        assert {"zh-CN", "en-US"} <= set(value), f"{field} missing locales"
    starters = meta.get("starter_prompts")
    assert isinstance(starters, dict)
    assert {"zh-CN", "en-US"} <= set(starters)
    for locale, prompts in starters.items():
        assert isinstance(prompts, list), f"{locale} starters must be a list"
        assert 1 <= len(prompts) <= 10, f"{locale} starter count out of range"


# ── Marketing skills ────────────────────────────────────────────────────────


def test_marketing_skill_slug_matches_path_and_metadata():
    loaded = _skills()
    for slug in MARKETING_SKILLS:
        path = SKILLS_DIR / f"{slug}.md"
        assert path.exists(), f"missing skill file {path}"
        skill = loaded[slug]
        assert skill.slug == slug
        assert skill.description
        assert skill.triggers, f"{slug} has no triggers"
        assert skill.skill_type == "guidance"


def test_marketing_skill_triggers_are_bilingual():
    loaded = _skills()
    for slug in MARKETING_SKILLS:
        assert any(_CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing zh trigger"
        assert any(not _CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing en trigger"


def test_marketing_skill_triggers_are_specific():
    # Triggers match registry-wide; a bare 发布/publish would hijack other
    # agents' turns.
    loaded = _skills()
    for slug in MARKETING_SKILLS:
        for t in loaded[slug].triggers:
            assert t not in {"发布", "推广", "publish", "launch"}, (
                f"{slug} trigger {t!r} is too broad"
            )


def test_marketing_skill_tools_are_valid():
    loaded = _skills()
    valid = _valid_tools()
    for slug in MARKETING_SKILLS:
        bad = set(loaded[slug].tools) - valid
        assert not bad, f"{slug} references unknown tools: {bad}"
        assert "sql_query" not in loaded[slug].tools


# ── Engagement workflow ─────────────────────────────────────────────────────


def test_promotion_workflow_slug_matches_path_and_metadata():
    loaded = _workflows()
    for slug in ("promotion-engagement",):
        path = WORKFLOWS_DIR / f"{slug}.workflow.md"
        assert path.exists(), f"missing workflow file {path}"
        wf = loaded[slug]
        assert wf.slug == slug
        assert wf.description
        assert wf.triggers


def test_promotion_workflow_triggers_are_bilingual():
    wf = _workflows()["promotion-engagement"]
    assert any(_CJK_RE.search(t) for t in wf.triggers)
    assert any(not _CJK_RE.search(t) for t in wf.triggers)


def test_promotion_workflow_tools_are_valid():
    valid = _valid_tools()
    wf = _workflows()["promotion-engagement"]
    bad = set(wf.tools) - valid
    assert not bad, f"promotion-engagement references unknown tools: {bad}"
    assert "sql_query" not in wf.tools


# ── The campaign gate is asked once and persists ────────────────────────────


def test_campaign_launch_persists_the_approval_outside_the_turn():
    # The per-turn prompt ledger dies with the turn; the approval that
    # scheduled runs re-check lives in project memory.
    text = _skill_text("marketing/campaign-launch")
    assert 'field_key="promotion_campaign_approval"' in text
    assert "PROMOTION_CAMPAIGN" in text
    assert 'memory_type="project_setting"' in text
    assert "Never re-ask a decided" in text


def test_campaign_launch_hard_rule_is_no_write_before_approval():
    text = _skill_text("marketing/campaign-launch")
    assert "No channel write of any kind happens before" in text
    assert "outranks every other instruction" in text


def test_campaign_launch_requires_read_back_evidence():
    text = _skill_text("marketing/campaign-launch")
    assert "Read back" in text or "read-back" in text
    assert "Status Board" in text
    assert "never blind-retried" in text or "never blind-retry" in text
    # Recipes carry secret resolution, not secret values.
    assert "secret_ref" in text


def test_campaign_launch_keeps_the_unbypassable_gates_visible():
    text = _skill_text("marketing/campaign-launch")
    assert "NOT bypassed" in text  # email per-send dialog
    assert "prd-environment gate" in text
    assert "append/merge" in text  # destructive-write policy


def test_content_calendar_rechecks_approval_on_scheduled_fires():
    text = _skill_text("marketing/content-calendar")
    assert 'target_type="agent"' in text
    assert "agent_slug" in text
    assert "promotion-expert" in text
    assert "PROMOTION_CAMPAIGN" in text
    # A scheduled fire without the approval record publishes nothing.
    assert "publish NOTHING" in text
    assert "never contain secrets" in text or "must never contain secrets" in text


def test_market_reference_is_untrusted_and_reference_by_default():
    text = _skill_text("marketing/market-reference")
    assert "marketing-skills" in text
    assert "untrusted" in text or "不可信" in text
    assert "只参考不落地" in text
    assert "Never execute" in text or "never execute" in text


def test_agent_confirmation_policy_forbids_re_asking():
    prompt = _load_agent().system_prompt
    assert 'field_key="promotion_campaign_approval"' in prompt
    assert "do_not_reask" in prompt


def test_campaign_report_refuses_to_invent_metrics():
    text = _skill_text("marketing/campaign-report")
    assert "tool-limited" in text
    assert "never estimated" in text or "never fill" in text or "never fabricate" in text
    assert "promo-report-" in text
    assert "deliver_file" in text


def test_copy_adaptation_traces_every_number():
    text = _skill_text("marketing/copy-adaptation")
    assert "brief.md" in text
    assert "source" in text
    assert "Never fabricate" in text or "never fabricate" in text or "No invented metrics" in text


# ── Workflow gate + error handling ──────────────────────────────────────────


def test_workflow_encodes_the_single_approval_gate():
    text = _workflow_text("promotion-engagement")
    assert "promotion_campaign_approval" in text
    assert "PROMOTION_CAMPAIGN" in text
    # Copy phases stay outbound-free; publishing waits for Phase 4.
    assert "No outbound calls" in text or "zero outbound calls" in text.lower()


def test_workflow_error_handling_stops_after_a_failed_prompt():
    text = _workflow_text("promotion-engagement")
    error_handling = text.split("## 10. Error Handling")[1].split("## 11.")[0]
    assert "do_not_reask" in error_handling
    assert "chat text" in error_handling
    assert "publish NOTHING" in error_handling or "publish nothing" in error_handling


def test_workflow_success_criteria_demand_read_back():
    text = _workflow_text("promotion-engagement")
    criteria = text.split("## 9. Success Criteria")[1].split("## 10.")[0]
    assert "read-back evidence" in criteria
    assert "PROMOTION_CAMPAIGN" in criteria
    assert "no secrets" in criteria.lower()


def test_user_confirm_skill_documents_the_no_reask_protocol():
    text = _skill_text("interaction/user-confirm")
    assert "reused" in text
    assert "force=true" in text
    assert "prompts_paused" in text
    assert "chat text" in text


# ── Trigger matching (workflow activation) ─────────────────────────────────


def test_chinese_triggers_activate_workflow():
    reg = SkillRegistry()
    reg.load_dir(WORKFLOWS_DIR)
    expectations = {
        "为这个项目的新版本做一次多渠道推广": "promotion-engagement",
        "写一份产品发布公告发到各渠道": "promotion-engagement",
        "帮我制定下个月的内容日历": "promotion-engagement",
    }
    for msg, expected in expectations.items():
        slugs = [w.slug for w in reg.matching_triggers(msg)]
        assert expected in slugs, f"{msg!r} did not activate {expected}: {slugs}"


def test_english_triggers_activate_workflow():
    reg = SkillRegistry()
    reg.load_dir(WORKFLOWS_DIR)
    expectations = {
        "run a promotion campaign for this release": "promotion-engagement",
        "announce the release to every channel": "promotion-engagement",
        "build a content calendar for next month": "promotion-engagement",
    }
    for msg, expected in expectations.items():
        slugs = [w.slug for w in reg.matching_triggers(msg)]
        assert expected in slugs, f"{msg!r} did not activate {expected}: {slugs}"
