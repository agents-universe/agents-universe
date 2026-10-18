"""Regression: scalar `tools:`/`skills:`/`workflows:` frontmatter must not break the runtime.

A hand-written or LLM-written agent file may declare `tools: "shell, filesystem"`
(a YAML scalar) instead of a YAML list. ``AgentConfig.from_file`` used to pass the
raw scalar through, and ``Agent.__init__`` iterated it per character in
``static_tool_names`` — so the agent silently lost every tool except ``plan_task``.
Follows the ``memory_rw._coerce_tags`` defense: a string is comma-split, not
treated as one opaque item.
"""
from __future__ import annotations

from pathlib import Path

from agent_core.agent import AgentConfig
from agent_core.tools.registry import _CORE_TOOLS, _OPTIONAL_TOOL_MODULES, build_tool_registry

_FRONTMATTER = """\
---
slug: "scalar-tools"
display_name: "Scalar Tools"
tools: {tools}
skills: {skills}
workflows: {workflows}
---

Body
"""


def _write_agent(tmp_path: Path, **meta: str) -> str:
    f = tmp_path / "scalar-tools.agent.md"
    f.write_text(
        _FRONTMATTER.format(
            tools=meta.get("tools", "[shell, filesystem]"),
            skills=meta.get("skills", "[code-review]"),
            workflows=meta.get("workflows", "[full-project-pentest]"),
        ),
        encoding="utf-8",
    )
    return str(f)


def _valid_tools() -> set[str]:
    return {tool.name for cls in _CORE_TOOLS for tool in (cls(),)} | set(_OPTIONAL_TOOL_MODULES)


def test_scalar_tools_comma_split(tmp_path: Path):
    cfg = AgentConfig.from_file(_write_agent(tmp_path, tools='"shell, filesystem"'))
    assert cfg.tools == ["shell", "filesystem"]


def test_scalar_tools_single_name_is_one_item(tmp_path: Path):
    cfg = AgentConfig.from_file(_write_agent(tmp_path, tools="shell"))
    assert cfg.tools == ["shell"]


def test_yaml_list_tools_unchanged(tmp_path: Path):
    cfg = AgentConfig.from_file(_write_agent(tmp_path, tools="[shell, filesystem]"))
    assert cfg.tools == ["shell", "filesystem"]


def test_scalar_skills_and_workflows_comma_split(tmp_path: Path):
    cfg = AgentConfig.from_file(
        _write_agent(
            tmp_path,
            skills='"code-review, knowledge-manager"',
            workflows="knowledge-ingestion",
        )
    )
    assert cfg.skills == ["code-review", "knowledge-manager"]
    assert cfg.workflows == ["knowledge-ingestion"]


def test_scalar_tools_resolve_to_valid_registry(tmp_path: Path):
    """A scalar list must build a usable tool registry (the real failure mode)."""
    cfg = AgentConfig.from_file(_write_agent(tmp_path, tools='"filesystem, shell"'))
    registry = build_tool_registry(cfg.tools)
    assert set(registry) == {"filesystem", "shell", "plan_task"}


def test_agent_static_tool_names_are_not_per_character(tmp_path: Path):
    """The regression: scalar tools must not iterate per character."""
    cfg = AgentConfig.from_file(_write_agent(tmp_path, tools='"shell, filesystem"'))
    static = [t for t in cfg.tools if not t.startswith("mcp")]
    # Before the fix this was ['s','h','e','l','l',',',' ','f',...].
    assert static == ["shell", "filesystem"]


def _write_raw(tmp_path: Path, extra_frontmatter: str) -> str:
    f = tmp_path / "raw-config.agent.md"
    f.write_text(
        f'---\nslug: "raw-config"\n{extra_frontmatter}\n---\n\nBody\n',
        encoding="utf-8",
    )
    return str(f)


def test_quoted_boolean_scalar_is_normalized(tmp_path: Path):
    """YAML keeps quoted scalars as strings; bool("false") is True and would
    silently disable api_request's write-confirmation gate."""
    cfg = AgentConfig.from_file(
        _write_raw(tmp_path, 'api_request_no_confirm: "false"')
    )
    assert cfg.api_request_no_confirm is False

    cfg = AgentConfig.from_file(
        _write_raw(tmp_path, 'api_request_no_confirm: "yes"')
    )
    assert cfg.api_request_no_confirm is True


def test_numeric_scalars_are_coerced_and_blank_falls_back(tmp_path: Path):
    cfg = AgentConfig.from_file(
        _write_raw(tmp_path, 'max_tokens: "64000"\ntoken_budget: 32000')
    )
    assert cfg.max_tokens == 64000
    assert cfg.token_budget == 32000

    # Blank YAML value -> None, malformed text -> string: both must fall back
    # instead of reaching min(max_tokens, MAX_OUTPUT_RESERVE) as non-ints.
    cfg = AgentConfig.from_file(_write_raw(tmp_path, "max_tokens:\ntoken_budget: bogus"))
    assert cfg.max_tokens == 128000
    assert cfg.token_budget == 100000
