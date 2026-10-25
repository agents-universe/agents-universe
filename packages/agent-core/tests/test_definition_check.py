"""Definition-file validation: what makes an agent/skill/workflow registrable."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.definition_check import check_definition


def _fm(front: str, body: str = "Body") -> str:
    return f"---\n{front}\n---\n\n{body}\n"


def _errors(result: dict) -> str:
    return " | ".join(result["errors"])


def _warnings(result: dict) -> str:
    return " | ".join(result["warnings"])


# --- agents -----------------------------------------------------------------


def test_valid_project_agent_passes():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "proj-a--helper"\ndisplay_name: "助手"\ndescription: "帮忙"\ntools:\n  - filesystem'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["expected"]["slug"] == "proj-a--helper"
    assert "suggested_frontmatter" not in result


def test_missing_project_prefix_is_error_with_concrete_fix():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "helper"\ndisplay_name: "助手"'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is False
    assert "'proj-a--'" in _errors(result)
    assert result["expected"]["slug"] == "proj-a--helper"
    assert "proj-a--helper" in result["suggested_frontmatter"]


def test_slug_must_equal_filename_stem():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "proj-a--other"\ndisplay_name: "x"'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is False
    assert "与文件名主干" in _errors(result)


def test_broken_yaml_reports_parse_error_and_suggestion():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "proj-a--helper"\ndescription: 负责 助手: 做事'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is False
    assert "YAML 解析失败" in _errors(result)
    assert result["suggested_frontmatter"].startswith("---\nslug: \"proj-a--helper\"")


def test_parse_error_does_not_also_report_missing_fields():
    """One root cause, one message — the YAML is the thing to fix."""
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('description: 负责 助手: 做事'),
        scope="project",
        project_slug="proj-a",
    )
    assert len(result["errors"]) == 1
    assert "YAML 解析失败" in result["errors"][0]
    assert result["warnings"] == []
    # The filename still yields a usable slug to suggest.
    assert result["expected"]["slug"] == "proj-a--helper"


def test_missing_slug_is_error():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('display_name: "助手"'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is False
    assert "缺少 slug" in _errors(result)
    # The filename still yields a usable suggestion.
    assert result["expected"]["slug"] == "proj-a--helper"


@pytest.mark.parametrize("slug", ["Helper", "helper_1", "helper.1", "helper x", "助手"])
def test_invalid_slug_charset_is_error(slug):
    result = check_definition(
        f"agents/proj-a--{slug}.agent.md",
        _fm(f'slug: "{slug}"\ndisplay_name: "x"'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is False
    assert "非法" in _errors(result)


def test_copied_slug_is_not_double_prefixed():
    """A definition copied from another project keeps only the target prefix."""
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "proj-b--helper"\ndisplay_name: "x"'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["expected"]["slug"] == "proj-a--helper"


def test_global_scope_has_no_prefix_rule():
    result = check_definition(
        "agents/tech-lead.agent.md",
        _fm('slug: "tech-lead"\ndisplay_name: "Tech Lead"'),
        scope="global",
    )
    assert result["ok"] is True


def test_misplaced_agent_paths_are_errors():
    root_level = check_definition(
        "helper.agent.md", _fm('slug: "helper"'), scope="project", project_slug="proj-a"
    )
    assert root_level["ok"] is False
    assert "不在 agents/ 目录下" in _errors(root_level)

    nested = check_definition(
        "agents/sub/proj-a--helper.agent.md",
        _fm('slug: "proj-a--helper"'),
        scope="project",
        project_slug="proj-a",
    )
    assert nested["ok"] is False
    assert "子目录" in _errors(nested)


def test_dot_slash_paths_are_not_misplaced():
    """Models write shell-style `./agents/...` paths; pathlib resolves them
    to the right file, so the string checks must too — a retained `./`
    prefix made every correctly-placed definition look misplaced (agent:
    false placement error, skill: false skills/ error, workflow: mangled
    expected slug)."""
    agent = check_definition(
        "./agents/proj-a--helper.agent.md",
        _fm('slug: "proj-a--helper"\ndisplay_name: "x"'),
        scope="project",
        project_slug="proj-a",
    )
    assert agent["ok"] is True, _errors(agent)
    assert agent["expected"]["slug"] == "proj-a--helper"

    skill = check_definition("./skills/foo.md", _fm('slug: "foo"'), scope="project")
    assert skill["ok"] is True, _errors(skill)

    workflow = check_definition(
        "./workflows/x.workflow.md", _fm('slug: "x"'), scope="project"
    )
    assert workflow["expected"]["slug"] == "x"
    assert workflow["warnings"] == []


def test_unknown_tool_is_warning_not_error():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "proj-a--helper"\ndisplay_name: "x"\ntools:\n  - filesystem\n  - made_up_tool'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is True
    assert "made_up_tool" in _warnings(result)


def test_every_registry_tool_resolves_to_its_declared_name():
    """A tool whose name is a property must not look unknown.

    ``memory_rw`` declares ``name`` as a property, so anything reading names
    off the class gets a property object — every real agent declaring it was
    warned about a tool that exists.
    """
    from agent_core.tools.registry import _CORE_TOOLS, known_static_tool_names

    known = known_static_tool_names()
    for cls in _CORE_TOOLS:
        assert cls().name in known, cls.__name__


def test_shipped_agent_definitions_declare_no_unknown_tools():
    """The framework's own agents must pass the check it shows to others."""
    framework_root = Path(__file__).resolve().parents[3]
    agents_dir = framework_root / "agents"
    if not agents_dir.is_dir():  # installed package without the repo checkout
        pytest.skip("framework agents/ dir not available")
    for path in sorted(agents_dir.glob("*.agent.md")):
        result = check_definition(
            path.relative_to(framework_root).as_posix(),
            path.read_text(encoding="utf-8"),
            scope="global",
            framework_root=str(framework_root),
        )
        assert result is not None and result["ok"], path.name
        unknown = [w for w in result["warnings"] if "不存在的工具" in w]
        assert not unknown, f"{path.name}: {unknown}"


def test_mcp_markers_are_known_tools():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "proj-a--helper"\ndisplay_name: "x"\ntools:\n  - mcp\n  - "mcp:jira"'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is True
    assert result["warnings"] == []


def test_scalar_list_field_warns_but_passes():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "proj-a--helper"\ndisplay_name: "x"\ntools: "filesystem, shell"'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is True
    assert "字符串标量" in _warnings(result)


def test_missing_display_name_warns():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "proj-a--helper"'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is True
    assert "display_name" in _warnings(result)


def test_locale_map_fields_do_not_warn():
    """Bilingual frontmatter (locale maps) is a first-class shape — it must
    not trip the missing-display_name or scalar-string warnings."""
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm(
            'slug: "proj-a--helper"\n'
            "display_name:\n  zh-CN: 助手\n  en-US: Helper\n"
            "description:\n  zh-CN: 帮忙\n  en-US: Helps out\n"
            "starter_prompts:\n  zh-CN: [帮我做事]\n  en-US: [Do my work]\n"
            "tools:\n  - filesystem"
        ),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is True
    assert result["warnings"] == []


def test_suggested_frontmatter_renders_canonical_of_locale_map():
    """A locale map must collapse to its zh canonical in the scalar template
    — never Python dict repr inside the suggested YAML."""
    result = check_definition(
        "agents/proj-a--bad.agent.md",
        _fm('slug: "proj-a--wrong"\ndisplay_name:\n  zh-CN: 助手\n  en-US: Helper'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is False
    suggestion = result["suggested_frontmatter"]
    assert '"助手"' in suggestion
    assert "{'zh-CN'" not in suggestion


# --- references -------------------------------------------------------------


def test_unresolved_references_warn_only_about_the_missing_ones(tmp_path):
    ws = tmp_path / "proj-a"
    fw = tmp_path / "fw"
    (ws / "skills").mkdir(parents=True)
    (ws / "skills" / "foo.md").write_text("x", encoding="utf-8")
    (fw / "agents" / "skills" / "knowledge").mkdir(parents=True)
    (fw / "agents" / "skills" / "knowledge" / "knowledge-manager.md").write_text("x", encoding="utf-8")
    (fw / "workflows").mkdir(parents=True)
    (fw / "workflows" / "wf.workflow.md").write_text("x", encoding="utf-8")

    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm(
            'slug: "proj-a--helper"\ndisplay_name: "x"\n'
            "skills:\n  - foo\n  - knowledge/knowledge-manager\n  - nope\n"
            "workflows:\n  - wf\n  - missing"
        ),
        scope="project",
        project_slug="proj-a",
        project_fs_path=str(ws),
        framework_root=str(fw),
    )
    assert result["ok"] is True
    joined = _warnings(result)
    assert "'nope'" in joined and "'missing'" in joined
    assert "'foo'" not in joined and "'wf'" not in joined


def test_references_are_not_checked_without_roots():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "proj-a--helper"\ndisplay_name: "x"\nskills:\n  - anything'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["ok"] is True
    assert result["warnings"] == []


# --- skills and workflows ---------------------------------------------------


def test_project_skill_slug_matches_relative_path():
    result = check_definition("skills/foo.md", _fm('slug: "foo"'), scope="project")
    assert result["ok"] is True
    assert result["warnings"] == []

    nested = check_definition("skills/imported/foo.md", _fm('slug: "imported/foo"'), scope="project")
    assert nested["ok"] is True
    assert nested["warnings"] == []


def test_project_skill_must_not_live_under_agents_skills():
    result = check_definition("agents/skills/foo.md", _fm('slug: "foo"'), scope="project")
    assert result["ok"] is False
    assert "skills/" in _errors(result)


def test_framework_skill_reads_are_not_flagged():
    """agents/skills/ is the framework layout — valid when read from there."""
    result = check_definition(
        "agents/skills/knowledge/knowledge-manager.md",
        _fm('slug: "knowledge/knowledge-manager"'),
        scope="global",
    )
    assert result is None


def test_workflow_stem_trap_warns():
    """x.workflow.md without a slug registers as 'x.workflow', matching nothing."""
    result = check_definition("workflows/x.workflow.md", _fm('description: "d"'), scope="project")
    assert result["ok"] is True
    assert "'x.workflow'" in _warnings(result)
    assert result["expected"]["slug"] == "x"


def test_workflow_suffix_convention_warns():
    result = check_definition("workflows/x.md", _fm('slug: "x"'), scope="project")
    assert result["ok"] is True
    assert ".workflow.md" in _warnings(result)
    assert result["expected"]["slug"] == "x"


def test_workflow_without_slug_and_wrong_suffix_suggests_a_usable_name():
    result = check_definition("workflows/x.md", _fm('description: "d"'), scope="project")
    assert result["ok"] is True
    assert "'x'" in _warnings(result) or "'x.workflow'" in _warnings(result)


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["knowledge/context.md", "tests/generated/test_x.py", "skills/_mixins/x.md", "docs/readme.md"],
)
def test_non_definition_paths_return_none(path):
    assert check_definition(path, "not frontmatter", scope="project") is None


def test_empty_workspace_path_has_no_prefix_rule():
    result = check_definition(
        "agents/helper.agent.md", _fm('slug: "helper"'), scope="project", project_slug=None
    )
    assert result["ok"] is True


def test_slug_is_normalized_into_a_suggestion():
    result = check_definition(
        "agents/proj-a--helper.agent.md",
        _fm('slug: "Helper_1"\ndisplay_name: "x"'),
        scope="project",
        project_slug="proj-a",
    )
    assert result["expected"]["slug"] == "proj-a--helper-1"
    assert Path(result["expected"]["path"]).name == "proj-a--helper-1.agent.md"
