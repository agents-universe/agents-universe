"""load_skills_from_dir must skip mixin/private paths at any depth.

Mixin fragments live in `_mixins/` (see api/main.py's mixin_dir), so a
name-only `startswith("_")` check registered them as standalone skills.
"""
from __future__ import annotations

from agent_core.skills.loader import load_skills_from_dir


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
