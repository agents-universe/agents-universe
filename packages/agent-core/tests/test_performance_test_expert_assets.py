"""Framework asset integrity tests for the performance-test-expert agent.

Mirrors ``test_pentest_expert_assets.py``: guards the contract between the
markdown assets and the runtime - slug/tool/skill references resolve,
bilingual triggers are present, the load channel stays within the
``script_writer`` contract, and the scope question is asked exactly once.
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

PERFORMANCE_SKILLS = [
    "performance/perf-scope-guard",
    "performance/perf-test-designer",
    "performance/perf-load-script-writer",
    "performance/perf-scenario-writeback",
    "performance/perf-report-writer",
    "performance/perf-report-pdf",
]
PERFORMANCE_WORKFLOWS = ["performance-test-engagement"]
KNOWN_REFERENCED_SKILLS = PERFORMANCE_SKILLS + [
    "integration/confluence-reader",
    "integration/jira-analyzer",
    "interaction/user-confirm",
    "knowledge/knowledge-manager",
]
KNOWN_REFERENCED_WORKFLOWS = [
    "performance-test-engagement",
    "test-artifact-and-jira-conventions",
    "knowledge-ingestion",
]
# Injected into every agent at response time via `public: true` in the skill
# file — never listed in this agent's frontmatter `skills:`.
PUBLIC_SKILLS = {"knowledge/knowledge-manager"}

_CJK_RE = re.compile(r"[一-鿿]")

# The 9 fixed report sections; the PDF converter keys off these headings.
REPORT_SECTIONS = [
    "Executive Summary",
    "Scope & Methodology",
    "Test Profile",
    "SLA & Targets",
    "Results",
    "Bottleneck Analysis",
    "Recommendations",
    "Appendix",
]


def _valid_tools() -> set[str]:
    # memory_rw exposes ``name`` as a property, so read it off an instance.
    return {tool.name for cls in _CORE_TOOLS for tool in (cls(),)} | set(_OPTIONAL_TOOL_MODULES)


def _load_agent() -> AgentConfig:
    return AgentConfig.from_file(str(AGENTS_DIR / "performance-test-expert.agent.md"))


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
    assert cfg.slug == "performance-test-expert"
    assert (AGENTS_DIR / f"{cfg.slug}.agent.md").exists()
    assert cfg.description


def test_agent_tools_valid_and_exclude_sql_query():
    cfg = _load_agent()
    valid = _valid_tools()
    assert set(cfg.tools) <= valid, f"unknown tools: {set(cfg.tools) - valid}"
    # sql_query reaches the platform's own DB (users/sessions/secrets) - never
    # a load-testing instrument.
    assert "sql_query" not in cfg.tools
    # Outbound probe requests must keep their per-request confirmation gate.
    assert cfg.api_request_no_confirm is False
    # Load runs go through script_writer; Playwright is explicitly out of scope.
    assert "script_writer" in cfg.tools
    assert "browser_playwright" not in cfg.tools
    assert "test_generator" not in cfg.tools


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
    wf_idx = cfg.system_prompt.index("performance-test-engagement")
    assert "plan_task" in cfg.system_prompt[wf_idx:]


# ── Performance skills ──────────────────────────────────────────────────────


def test_performance_skill_slug_matches_path_and_metadata():
    loaded = _skills()
    for slug in PERFORMANCE_SKILLS:
        path = SKILLS_DIR / f"{slug}.md"
        assert path.exists(), f"missing skill file {path}"
        skill = loaded[slug]
        assert skill.slug == slug
        assert skill.description
        assert skill.triggers, f"{slug} has no triggers"
        assert skill.skill_type == "guidance"


def test_performance_skill_triggers_are_bilingual():
    loaded = _skills()
    for slug in PERFORMANCE_SKILLS:
        assert any(_CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing zh trigger"
        assert any(not _CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing en trigger"


def test_performance_skill_tools_are_valid():
    loaded = _skills()
    valid = _valid_tools()
    for slug in PERFORMANCE_SKILLS:
        bad = set(loaded[slug].tools) - valid
        assert not bad, f"{slug} references unknown tools: {bad}"


def test_performance_assets_never_use_sql_query():
    loaded = {**_skills(), **_workflows()}
    for slug in PERFORMANCE_SKILLS + PERFORMANCE_WORKFLOWS:
        assert "sql_query" not in loaded[slug].tools, f"{slug} must not use sql_query"


# ── Engagement workflow ─────────────────────────────────────────────────────


def test_performance_workflow_slug_matches_path_and_metadata():
    loaded = _workflows()
    for slug in PERFORMANCE_WORKFLOWS:
        path = WORKFLOWS_DIR / f"{slug}.workflow.md"
        assert path.exists(), f"missing workflow file {path}"
        wf = loaded[slug]
        assert wf.slug == slug
        assert wf.description
        assert wf.triggers


def test_performance_workflow_triggers_are_bilingual():
    loaded = _workflows()
    for slug in PERFORMANCE_WORKFLOWS:
        assert any(_CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing zh trigger"
        assert any(not _CJK_RE.search(t) for t in loaded[slug].triggers), f"{slug} missing en trigger"


def test_performance_workflow_tools_are_valid():
    loaded = _workflows()
    valid = _valid_tools()
    for slug in PERFORMANCE_WORKFLOWS:
        bad = set(loaded[slug].tools) - valid
        assert not bad, f"{slug} references unknown tools: {bad}"


# ── The scope question is asked once ───────────────────────────────────────


def test_scope_guard_checks_an_existing_declaration_before_asking():
    text = _skill_text("performance/perf-scope-guard")
    assert "engagement.md" in text
    assert "PERF_SCOPE" in text
    assert 'memory_type="project_setting"' in text
    assert "Never re-ask a decided field" in text


def test_scope_guard_asks_one_question_on_a_stable_field_key():
    text = _skill_text("performance/perf-scope-guard")
    assert 'field_key="perf_scope_env"' in text
    # Environment, endpoints, ceiling and duration in one compound dialog is
    # what makes the agent come back for the parts it could not fit.
    assert "compound dialog" in text


def test_scope_guard_keeps_production_confirmation_separate():
    text = _skill_text("performance/perf-scope-guard")
    assert "perf_prod_load_authorization" in text
    assert "never re-asks which environment" in text


def test_scope_guard_falls_back_to_chat_text_after_a_failed_prompt():
    text = _skill_text("performance/perf-scope-guard")
    assert "do_not_reask" in text
    assert "chat text" in text


def test_scope_guard_hard_refusals_bound_the_load():
    text = _skill_text("performance/perf-scope-guard")
    assert "Hard Refusal List" in text
    assert "outside the declared scope" in text
    assert "ceiling" in text


def test_workflow_reuses_the_declaration_and_stops_after_a_failed_prompt():
    text = _workflow_text("performance-test-engagement")
    assert "PERF_SCOPE" in text
    assert "perf_scope_env" in text
    error_handling = text.split("## 10. Error Handling")[1].split("## 11.")[0]
    assert "do_not_reask" in error_handling
    assert "chat text" in error_handling


def test_agent_confirmation_policy_forbids_re_asking():
    prompt = _load_agent().system_prompt
    assert 'field_key="perf_scope_env"' in prompt
    assert "do_not_reask" in prompt
    assert 'field_key="perf_scope"' not in prompt


def test_perf_assets_can_persist_the_declaration_outside_the_turn():
    # The per-turn prompt ledger dies with the turn; a decision that has to
    # hold for the whole engagement lives in project memory.
    assert "memory_rw" in _load_agent().tools
    assert "memory_rw" in _skills()["performance/perf-scope-guard"].tools
    assert "memory_rw" in _workflows()["performance-test-engagement"].tools


def test_user_confirm_skill_documents_the_no_reask_protocol():
    text = _skill_text("interaction/user-confirm")
    assert "reused" in text
    assert "force=true" in text
    assert "prompts_paused" in text
    assert "chat text" in text


# ── Load-script channel contract ────────────────────────────────────────────


def test_load_script_skill_encodes_the_bounded_channel_contract():
    text = _skill_text("performance/perf-load-script-writer")
    assert "script_writer" in text
    assert "AGENT_SECRET_" in text
    # The 240s self-imposed bound inside the 300s executor cap.
    assert "240" in text
    # The verdict must survive the 4000-char stdout tail: printed last.
    assert "stdout_tail" in text
    assert "VERDICT" in text
    assert "asyncio" in text
    assert "httpx" in text
    # No fetch-by-run-id: a status:"running" result is tool-limited, not a poll.
    assert "status:\"running\"" in text or "status:\"running\"" in text.replace("'", '"')
    assert "performance/results/" in text


def test_scenario_designer_bounds_every_profile():
    text = _skill_text("performance/perf-test-designer")
    assert "≤240 s" in text or "≤ 240 s" in text
    for scenario in ("smoke", "baseline", "spike", "soak-lite"):
        assert scenario in text, f"missing scenario {scenario}"
    assert "percentile" in text.lower()
    assert "performance/scenario-design-" in text


# ── Jira/Confluence writeback ───────────────────────────────────────────────


def test_writeback_picks_one_target_and_verifies_by_read_back():
    text = _skill_text("performance/perf-scenario-writeback")
    assert "[AI perf]" in text
    assert "create_test_issue" in text  # documented as the thing NOT to use
    assert "never `create_test_issue`" in text or "never create_test_issue" in text
    assert "Read back" in text or "read back" in text.lower()
    assert "create_page" in text
    assert "body_format" in text


# ── Report: 9 sections + MD/PDF dual delivery ──────────────────────────────


def test_report_writer_carries_nine_sections_and_dual_delivery():
    text = _skill_text("performance/perf-report-writer")
    for section in REPORT_SECTIONS:
        assert section in text, f"report-writer missing section {section!r}"
    assert "performance/perf-report-" in text
    assert "agents/skills/performance/perf-report-pdf.md" in text
    assert text.count("deliver_file") >= 2
    # Percentiles, not averages — the headline discipline.
    assert "p95" in text


def test_report_pdf_skill_ships_one_self_contained_converter_script():
    text = _skill_text("performance/perf-report-pdf")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    assert len(blocks) == 1, f"expected 1 converter script block, got {len(blocks)}"
    script = blocks[0]
    for token in ("pypdf", "STSong-Light", "multiBuild", "MD_PATH", "OUT_PATH", "performance/"):
        assert token in script, f"converter script missing {token!r}"
    # PDF lands in the workspace performance/ dir — never OUTPUT_DIR (which
    # would auto-deliver a duplicate attachment ahead of the deliver_file pair).
    assert "OUTPUT_DIR" not in script
    # The script source itself must not contain a literal markdown fence.
    assert script.count("```") == 0
    # Perf-specific verification: section headings + the p95 token.
    assert "p95" in script
    assert "section headings missing" in script


# ── Trigger matching (workflow activation) ─────────────────────────────────


def test_chinese_triggers_activate_workflow():
    reg = SkillRegistry()
    reg.load_dir(WORKFLOWS_DIR)
    expectations = {
        "对订单接口做一次压测": "performance-test-engagement",
        "帮我做个性能测试": "performance-test-engagement",
        "设计负载测试场景": "performance-test-engagement",
    }
    for msg, expected in expectations.items():
        slugs = [w.slug for w in reg.matching_triggers(msg)]
        assert expected in slugs, f"{msg!r} did not activate {expected}: {slugs}"


def test_english_triggers_activate_workflow():
    reg = SkillRegistry()
    reg.load_dir(WORKFLOWS_DIR)
    expectations = {
        "run a load test on the order api": "performance-test-engagement",
        "please do a performance test": "performance-test-engagement",
        "stress test the login endpoint": "performance-test-engagement",
    }
    for msg, expected in expectations.items():
        slugs = [w.slug for w in reg.matching_triggers(msg)]
        assert expected in slugs, f"{msg!r} did not activate {expected}: {slugs}"
