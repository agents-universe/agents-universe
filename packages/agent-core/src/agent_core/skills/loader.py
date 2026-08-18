"""Skill and Workflow Markdown loader."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter


@dataclass
class SkillDefinition:
    slug: str
    skill_type: str  # guidance | template | executable | composite
    description: str
    triggers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    mixins: list[str] = field(default_factory=list)
    inputs: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)  # for composite
    body: str = ""  # full markdown body (instructions)
    execution_code: str | None = None  # extracted from ## Execution code block
    file_path: str = ""


_CODE_BLOCK_RE = re.compile(
    r"##\s+Execution\s*\n+```(\w+)\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_MIXIN_INCLUDE_RE = re.compile(r"\[\[(_mixins/[^\]]+)\]\]")


def _as_list(value: object) -> list:
    """Normalize a YAML scalar to a one-item list.

    frontmatter authors write `triggers: code-review` (a scalar) as often as
    `triggers: [code-review]`. List-typed fields must not stay strings — a
    str is iterated per character (`matching_triggers` would match any message
    containing any letter of the trigger) and `x in str` degenerates to
    substring matching.
    """
    if isinstance(value, str):
        return [value]
    return list(value) if isinstance(value, (list, tuple)) else []


def load_skill(file_path: str | Path, mixin_dir: str | Path | None = None) -> SkillDefinition:
    """Parse a skill or workflow Markdown file into a SkillDefinition."""
    path = Path(file_path)
    post = frontmatter.load(str(path))
    meta = post.metadata
    body = post.content

    # Extract executable code block if present
    execution_code = None
    code_match = _CODE_BLOCK_RE.search(body)
    if code_match:
        execution_code = code_match.group(2).strip()

    # Inline mixins if mixin_dir is provided. _as_list normalizes a scalar
    # (`mixins: _mixins/git`) to a one-item list — iterating the raw value
    # would expand a str per character (`m`, `.`, `m`, ...) and silently
    # resolve no mixin at all.
    mixins = _as_list(meta.get("mixins"))
    if mixin_dir and mixins:
        from agent_core.paths import is_within
        mixin_base = Path(mixin_dir)
        for mixin_slug in mixins:
            mixin_path = (mixin_base / f"{mixin_slug.removeprefix('_mixins/')}.md").resolve()
            # frontmatter is agent-writable; a mixins value like
            # "../../other-slug/knowledge/api-map" would resolve OUTSIDE the
            # skills directory and splice a sibling project's knowledge into
            # the LLM context — cross-project read. Refuse anything that
            # escapes the mixin directory.
            if not is_within(mixin_base, mixin_path):
                continue
            if mixin_path.exists():
                mixin_post = frontmatter.load(str(mixin_path))
                body = body + f"\n\n---\n{mixin_post.content}"

    return SkillDefinition(
        slug=meta.get("slug", path.stem),
        skill_type=meta.get("type", "guidance"),
        description=meta.get("description", ""),
        triggers=_as_list(meta.get("triggers")),
        tools=_as_list(meta.get("tools")),
        mixins=_as_list(meta.get("mixins")),
        inputs=_as_list(meta.get("inputs")),
        steps=_as_list(meta.get("steps")),
        body=body,
        execution_code=execution_code,
        file_path=str(path),
    )


def load_skills_from_dir(
    skills_dir: str | Path,
    mixin_dir: str | Path | None = None,
    recursive: bool = True,
) -> list[SkillDefinition]:
    """Load all skills from a directory (optionally recursive)."""
    root = Path(skills_dir)
    if not root.exists():
        return []

    pattern = "**/*.md" if recursive else "*.md"
    skills = []
    for md_file in sorted(root.glob(pattern)):
        if md_file.name.startswith("_"):
            continue  # skip mixins and private files
        try:
            skill = load_skill(md_file, mixin_dir=mixin_dir)
            skills.append(skill)
        except Exception as e:
            import logging
            logging.getLogger("agent_core").warning("Failed to load skill %s: %s", md_file.name, e)
    return skills
