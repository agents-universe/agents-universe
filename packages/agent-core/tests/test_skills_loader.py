"""load_skills_from_dir must skip mixin/private paths at any depth.

Mixin fragments live in `_mixins/` (see api/main.py's mixin_dir), so a
name-only `startswith("_")` check registered them as standalone skills.
"""
from __future__ import annotations

from agent_core.skills.loader import load_skill, load_skills_from_dir


def _write(path, slug: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nslug: {slug}\ndescription: {slug} skill\n---\nBODY\n", encoding="utf-8"
    )


def _tree(tmp_path):
    root = tmp_path / "skills"
    _write(root / "testing" / "real-skill.md", "real-skill")
    _write(root / "_mixins" / "shared-rules.md", "shared-rules")
    _write(root / "testing" / "_private" / "helper.md", "helper")
    _write(root / "_draft.md", "draft")
    return root


def test_underscore_directory_is_skipped(tmp_path):
    slugs = {s.slug for s in load_skills_from_dir(_tree(tmp_path))}
    assert slugs == {"real-skill"}


def test_non_recursive_mode_ignores_subdirectories(tmp_path):
    slugs = {s.slug for s in load_skills_from_dir(_tree(tmp_path), recursive=False)}
    assert slugs == set()  # only _draft.md sits at the root, and it is private


def test_mixin_dir_still_resolves_when_explicit(tmp_path):
    """Skipping _mixins/ as a skill must not stop mixin inlining."""
    root = _tree(tmp_path)
    (root / "_mixins" / "shared-rules.md").write_text(
        "---\nslug: shared-rules\n---\nMIXIN RULES\n", encoding="utf-8"
    )
    (root / "testing" / "uses-mixin.md").write_text(
        "---\nslug: uses-mixin\nmixins: [_mixins/shared-rules]\n---\nSKILL BODY\n",
        encoding="utf-8",
    )
    skills = {s.slug: s for s in load_skills_from_dir(root, mixin_dir=root / "_mixins")}
    assert "shared-rules" not in skills
    assert "SKILL BODY" in skills["uses-mixin"].body
    assert "MIXIN RULES" in skills["uses-mixin"].body


def _write_with_frontmatter(path, extra: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nslug: {path.stem}\n{extra}---\nBODY\n", encoding="utf-8")


def test_public_flag_true(tmp_path):
    path = tmp_path / "pub.md"
    _write_with_frontmatter(path, "public: true\n")
    assert load_skill(path).public is True


def test_public_flag_defaults_false(tmp_path):
    path = tmp_path / "plain.md"
    _write_with_frontmatter(path, "description: no flag\n")
    assert load_skill(path).public is False


def test_public_flag_quoted_false_stays_false(tmp_path):
    # A quoted YAML scalar is a non-empty string — naive bool() would be True.
    path = tmp_path / "quoted.md"
    _write_with_frontmatter(path, 'public: "false"\n')
    assert load_skill(path).public is False


def test_scalar_description_keeps_i18n_empty(tmp_path):
    path = tmp_path / "scalar.md"
    _write_with_frontmatter(path, "description: plain one-liner\n")
    skill = load_skill(path)
    assert skill.description == "plain one-liner"
    assert skill.description_i18n == {}


def test_locale_map_description_is_en_first(tmp_path):
    """Skill/workflow descriptions are English-first: the LLM-injected
    canonical must be the en entry, byte-identical to the pre-i18n scalar."""
    en = "Profile any dataset before analysis — schema, distributions, gaps."
    path = tmp_path / "mapped.md"
    path.write_text(
        "---\n"
        "slug: mapped\n"
        "description:\n"
        f'  en-US: "{en}"\n'
        '  zh-CN: "分析前先摸清数据：结构、分布、缺失。"\n'
        "---\nBODY\n",
        encoding="utf-8",
    )
    skill = load_skill(path)
    assert skill.description == en
    assert skill.description_i18n == {
        "en-US": en,
        "zh-CN": "分析前先摸清数据：结构、分布、缺失。",
    }


def test_workflow_map_description_keeps_llm_prompt_bytes(tmp_path):
    """Regression guard for the only LLM injection of a description
    (agent.py `## Available Workflows`): the canonical string must stay the
    exact English text even when the frontmatter is a locale map."""
    en = "Full ad-hoc data analysis pipeline — clarify the question, deliver."
    wf = tmp_path / "ad-hoc.workflow.md"
    wf.write_text(
        "---\n"
        "slug: ad-hoc\n"
        "description:\n"
        f'  en-US: "{en}"\n'
        '  zh-CN: "即席分析全链路。"\n'
        "---\nSTEPS\n",
        encoding="utf-8",
    )
    assert load_skill(wf).description == en


def test_non_locale_dict_description_degrades_to_empty(tmp_path):
    """A structured dict with non-locale keys must not str()-leak into the
    description; the loader degrades to empty instead of raising."""
    path = tmp_path / "bad.md"
    path.write_text(
        "---\nslug: bad\ndescription:\n  slug: x\n---\nBODY\n", encoding="utf-8"
    )
    skill = load_skill(path)
    assert skill.description == ""
    assert skill.description_i18n == {}
