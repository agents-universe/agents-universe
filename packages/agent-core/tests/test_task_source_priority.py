"""Tests for the per-turn Task Source Priority routing directive.

The directive is table-driven: entries live in the `routing` frontmatter of
the task-source-priority skill, so the code must not hardcode tool names.
"""
from __future__ import annotations

import re
from pathlib import Path

from agent_core.agent import Agent, AgentConfig
from agent_core.skills.loader import SkillDefinition, load_skill
from agent_core.skills.registry import SkillRegistry
from agent_core.tools.base import ToolContext

REPO_ROOT = Path(__file__).resolve().parents[3]
ROUTING_SKILL_PATH = (
    REPO_ROOT / "agents" / "skills" / "integration" / "task-source-priority.md"
)

_DEFAULT_ROUTING = [
    {
        "id": "jira-card",
        "priority": 10,
        "label": "a Jira card",
        "anchors": [r"(?<![A-Za-z0-9])[A-Z]{2,}-\d+(?![A-Za-z0-9])"],
        "tool": "jira",
        "first_ops": ["get_issue", "get_comments", "get_transitions"],
        "follow_ups": [
            {
                "tool": "github",
                "op": "search_by_jira_key",
                "note": "to find the card's PRs, then get_pr_detail on each",
            }
        ],
    },
    {
        "id": "pull-request",
        "priority": 20,
        "label": "a pull request",
        "anchors": [
            r"(?<![A-Za-z0-9])pull\s+request(?![A-Za-z0-9])",
            r"(?<![A-Za-z0-9])pr(?![A-Za-z0-9])",
            r"/pull/\d+",
            r"合并请求",
        ],
        "tool": "github",
        "first_ops": ["get_pr_detail", "list_prs"],
        "follow_ups": [
            {
                "tool": "jira",
                "op": "jira-analyzer",
                "note": "on the Jira key found in the PR title, branch, or commits",
            }
        ],
    },
]


def _registry_with_routing(routing: list[dict] | None = None) -> SkillRegistry:
    reg = SkillRegistry()
    reg._skills["integration/task-source-priority"] = SkillDefinition(
        slug="integration/task-source-priority",
        skill_type="guidance",
        description="task-type → authoritative-source priority",
        routing=routing or _DEFAULT_ROUTING,
        body="",
    )
    return reg


def _make_agent(tool_names: list[str], routing: list[dict] | None = None) -> Agent:
    config = AgentConfig(
        slug="test-agent",
        description="test",
        system_prompt="You are a test agent.",
        tools=tool_names,
    )
    tool_context = ToolContext(
        project_id="p1",
        project_fs_path="C:/projects/p1",
        conversation_id="c1",
        user_id="u1",
    )
    return Agent(
        config=config,
        credentials={},
        tier_models={},
        skill_registry=_registry_with_routing(routing),
        tool_context=tool_context,
    )


# ── Directive generation ────────────────────────────────────────────────────


def test_jira_key_message_maps_to_jira_first():
    agent = _make_agent(["jira", "github", "git_repo"])
    directive = agent._task_source_priority("帮我对 QA-123 这张卡生成自动化测试")

    assert directive is not None
    assert "Call the `jira` tool FIRST: get_issue → get_comments → get_transitions" in directive
    # github follow-up present, and no local-repo preamble allowed
    assert "use `github`: search_by_jira_key" in directive
    assert "list_repos/status/pull" in directive


def test_jira_key_adjacent_to_cjk_still_matches():
    # \b would fail between CJK and ASCII — the custom boundary must not.
    agent = _make_agent(["jira", "github"])
    directive = agent._task_source_priority("请分析下对QA-123这张卡的影响范围")
    assert directive is not None
    assert "`jira` tool FIRST" in directive


def test_pr_message_maps_to_github_first():
    agent = _make_agent(["jira", "github", "git_repo"])
    directive = agent._task_source_priority("帮我 review 这个 PR https://ghe.example.com/org/repo/pull/456")

    assert directive is not None
    assert "Call the `github` tool FIRST: get_pr_detail → list_prs" in directive
    assert "use `jira`: jira-analyzer" in directive


def test_chinese_pr_phrase_maps_to_github_first():
    agent = _make_agent(["jira", "github", "git_repo"])
    directive = agent._task_source_priority("帮我处理这个合并请求")
    assert directive is not None
    assert "`github` tool FIRST" in directive


def test_jira_key_and_pr_anchor_prefers_pr():
    # The PR is the object of action; the Jira key is context.
    agent = _make_agent(["jira", "github", "git_repo"])
    directive = agent._task_source_priority("review PR #42 (QA-123 关联的 PR)")

    assert directive is not None
    assert "`github` tool FIRST" in directive
    assert "`jira` tool FIRST" not in directive


def test_no_directive_for_unrelated_messages():
    agent = _make_agent(["jira", "github", "git_repo"])
    assert agent._task_source_priority("生成一份PPT") is None
    assert agent._task_source_priority("帮我查一下昨天的知识") is None


def test_directive_gated_on_tool_availability():
    # office-assistant-like tool set: no jira/github/git_repo → nothing.
    agent = _make_agent(["filesystem", "knowledge_rw"])
    assert agent._task_source_priority("帮我对 QA-123 这张卡生成自动化测试") is None
    assert agent._task_source_priority("review this PR") is None

    # jira without github: jira-card directive keeps the jira part, drops the
    # github follow-up; PR directive entirely absent.
    agent = _make_agent(["jira", "git_repo"])
    directive = agent._task_source_priority("帮我对 QA-123 生成自动化测试")
    assert directive is not None
    assert "use `github`" not in directive
    assert agent._task_source_priority("review this PR") is None


def test_directive_is_table_driven_and_extensible():
    # A new platform (GitLab issues) is one routing entry — no code change in
    # the routing layer. The new tool's own module/registry entry is the
    # platform implementation, outside this mechanism.
    routing = _DEFAULT_ROUTING + [
        {
            "id": "git-issue",
            "priority": 30,
            "label": "a GitLab issue",
            "anchors": [r"(?<![A-Za-z0-9])issue\s+#?\d+(?![A-Za-z0-9])", r"问题\s*#?\d+"],
            "tool": "gitlab",
            "first_ops": ["get_issue", "get_comments"],
            "follow_ups": [],
        }
    ]
    agent = _make_agent([], routing=routing)
    agent._tools["gitlab"] = object()  # simulate the future tool being attached
    directive = agent._task_source_priority("帮我处理 issue #5")

    assert directive is not None
    assert "a GitLab issue" in directive
    assert "Call the `gitlab` tool FIRST: get_issue → get_comments" in directive


def test_missing_routing_skill_yields_no_directive():
    agent = _make_agent(["jira", "github"])
    agent._skill_registry = SkillRegistry()  # no routing skill loaded
    assert agent._task_source_priority("帮我对 QA-123 生成自动化测试") is None


# ── Asset integrity (the real routing skill file) ───────────────────────────


def test_routing_skill_file_parses_with_valid_entries():
    skill = load_skill(str(ROUTING_SKILL_PATH))

    assert skill.slug == "integration/task-source-priority"
    assert skill.description
    assert skill.triggers == []  # must never be trigger-injected into other agents
    assert len(skill.routing) >= 2

    for entry in skill.routing:
        for key in ("id", "priority", "label", "tool", "anchors", "first_ops"):
            assert key in entry, f"routing entry missing {key!r}: {entry}"
        for anchor in entry["anchors"]:
            re.compile(anchor)  # every anchor must be a valid regex
        assert entry["tool"] in ("jira", "github", "git_repo", "gitlab")


def test_real_routing_skill_drives_the_directive():
    skill = load_skill(str(ROUTING_SKILL_PATH))
    agent = _make_agent(["jira", "github", "git_repo"], routing=skill.routing)

    directive = agent._task_source_priority("帮我对 QA-123 这张卡生成自动化测试")
    assert directive is not None
    assert "`jira` tool FIRST" in directive

    directive = agent._task_source_priority("帮我处理这个合并请求")
    assert directive is not None
    assert "`github` tool FIRST" in directive
