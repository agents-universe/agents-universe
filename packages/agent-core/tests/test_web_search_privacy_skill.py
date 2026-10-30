"""web-search-privacy skill: loads clean, triggers bilingually, activates on PII queries.

The restriction lives in a guidance skill (not code): trigger matching injects
the body whenever the user message mentions personal-information topics, and
the body itself grants the technical-context exemption (regex/validation/
format queries about PII fields are allowed).
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_core.skills.loader import load_skill
from agent_core.skills.registry import SkillRegistry
from agent_core.tools.registry import _CORE_TOOLS, _OPTIONAL_TOOL_MODULES

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_PATH = REPO_ROOT / "agents" / "skills" / "interaction" / "web-search-privacy.md"
SLUG = "interaction/web-search-privacy"

_CJK_RE = re.compile(r"[一-鿿]")


def _skill():
    return load_skill(SKILL_PATH)


def test_slug_matches_path_and_metadata():
    skill = _skill()
    assert skill.slug == SLUG
    assert skill.skill_type == "guidance"
    assert skill.description
    assert skill.description_i18n.get("zh-CN")


def test_triggers_are_bilingual():
    skill = _skill()
    assert skill.triggers
    assert any(_CJK_RE.search(t) for t in skill.triggers), "missing zh trigger"
    assert any(not _CJK_RE.search(t) for t in skill.triggers), "missing en trigger"


def test_tools_are_valid():
    valid = {tool.name for cls in _CORE_TOOLS for tool in (cls(),)} | set(_OPTIONAL_TOOL_MODULES)
    assert set(_skill().tools) == {"web_search"}
    assert set(_skill().tools) <= valid


def test_pii_queries_activate_skill():
    reg = SkillRegistry()
    reg.load_dir(SKILL_PATH.parent.parent)
    expectations = {
        "帮我查一下这个人的手机号": SLUG,
        "搜索张三的身份证号": SLUG,
        "look up John Doe's home address": SLUG,
        "find the SSN behind this handle": SLUG,
    }
    for msg, expected in expectations.items():
        slugs = [s.slug for s in reg.matching_triggers(msg)]
        assert expected in slugs, f"{msg!r} did not activate {expected}: {slugs}"


def test_technical_context_queries_still_match_and_body_exempts_them():
    """A technical query (手机号正则) still matches the trigger — the body's
    exemption section is what allows it, so the body must carry the rule."""
    reg = SkillRegistry()
    reg.load_dir(SKILL_PATH.parent.parent)
    slugs = [s.slug for s in reg.matching_triggers("手机号正则怎么写")]
    assert SLUG in slugs

    body = _skill().body
    assert "Exempt" in body
    assert "regex" in body.lower()
    assert "validation" in body.lower()
