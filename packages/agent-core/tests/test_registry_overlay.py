"""SkillRegistry overlay semantics for project-scoped skills/workflows."""
from __future__ import annotations

from pathlib import Path

from agent_core.skills.registry import SkillRegistry


def _write_skill(dir_: Path, slug: str, body: str, trigger: str = "") -> None:
    path = dir_ / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = f'slug: "{slug}"\ndescription: "{slug} desc"\n'
    if trigger:
        meta += f'triggers: ["{trigger}"]\n'
    path.write_text(f"---\n{meta}---\n\n{body}\n", encoding="utf-8")


def test_overlay_project_shadows_global_without_mutating_base(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "project"
    _write_skill(global_dir, "shared", "GLOBAL BODY")
    _write_skill(global_dir, "global-only", "GLOBAL ONLY")
    _write_skill(project_dir, "shared", "PROJECT BODY")
    _write_skill(project_dir, "project-only", "PROJECT ONLY")

    base = SkillRegistry()
    base.load_dir(global_dir)

    overlay = base.overlay()
    overlay.load_dir(project_dir)

    # Project definitions win for the same slug.
    assert overlay.get("shared").body.strip() == "PROJECT BODY"
    # Global-only and project-only are both visible.
    assert overlay.get("global-only").body.strip() == "GLOBAL ONLY"
    assert overlay.get("project-only").body.strip() == "PROJECT ONLY"
    # Base registry is not polluted.
    assert base.get("shared").body.strip() == "GLOBAL BODY"
    assert base.get("project-only") is None


def test_overlay_trigger_matching_sees_project_skills(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "project"
    _write_skill(global_dir, "shared", "GLOBAL BODY", trigger="global-trigger")
    _write_skill(project_dir, "shared", "PROJECT BODY", trigger="project-trigger")

    base = SkillRegistry()
    base.load_dir(global_dir)
    overlay = base.overlay()
    overlay.load_dir(project_dir)

    matched = overlay.matching_triggers("please run project-trigger")
    slugs = [s.slug for s in matched]
    assert "shared" in slugs
    # The project definition's body is what gets injected.
    matched_shared = next(s for s in matched if s.slug == "shared")
    assert matched_shared.body.strip() == "PROJECT BODY"

    # Global trigger no longer matches on the overlay (project replaced it).
    assert all(s.slug != "shared" for s in overlay.matching_triggers("global-trigger"))
    # ...but the base registry still matches it.
    assert any(s.slug == "shared" for s in base.matching_triggers("global-trigger"))


def test_overlay_with_empty_project_dir_preserves_global(tmp_path):
    global_dir = tmp_path / "global"
    project_dir = tmp_path / "project"  # does not exist yet
    _write_skill(global_dir, "keep-me", "KEEP")

    base = SkillRegistry()
    base.load_dir(global_dir)
    overlay = base.overlay()
    overlay.load_dir(project_dir)  # no-op, must not clear anything

    assert overlay.get("keep-me").body.strip() == "KEEP"
