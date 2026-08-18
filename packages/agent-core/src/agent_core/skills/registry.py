"""Skill and Workflow registry — loads once, serves on demand."""
from __future__ import annotations

from pathlib import Path

from .loader import SkillDefinition, load_skill, load_skills_from_dir


class SkillRegistry:
    """In-memory registry of skills and workflows.

    Project-level skills override global skills with the same slug.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillDefinition] = {}

    def overlay(self) -> "SkillRegistry":
        """Return a shallow copy for project overlays.

        Loading project skills into the copy shadows global definitions with
        the same slug without mutating this registry.
        """
        clone = SkillRegistry()
        clone._skills = dict(self._skills)
        return clone

    def load_dir(
        self,
        skills_dir: str | Path,
        mixin_dir: str | Path | None = None,
    ) -> None:
        """Load (or reload) skills from a directory. Project overrides come last."""
        for skill in load_skills_from_dir(skills_dir, mixin_dir=mixin_dir):
            self._skills[skill.slug] = skill

    def get(self, slug: str) -> SkillDefinition | None:
        return self._skills.get(slug)

    def all(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def matching_triggers(self, text: str) -> list[SkillDefinition]:
        """Return skills whose trigger phrases appear in text (case-insensitive).

        Also handles explicit /slug commands (e.g. "/code-review ...").
        """
        matched: list[SkillDefinition] = []
        text_lower = text.lower()

        # Explicit /command prefix — highest priority
        if text_lower.startswith("/"):
            cmd_slug = text_lower.split()[0].lstrip("/")
            explicit = self._skills.get(cmd_slug)
            if explicit:
                matched.append(explicit)

        # Trigger-based matching
        for s in self._skills.values():
            if s in matched:
                continue
            # A blank trigger (frontmatter typo like `triggers: [""]`) is
            # substring-matched by every message — require a non-blank trigger.
            if any(str(t).strip() and str(t).lower() in text_lower for t in s.triggers):
                matched.append(s)

        return matched
