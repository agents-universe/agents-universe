"""Agent roster discovery for delegation.

An agent that lacks a capability hands the subtask to another agent. To do that
it must first see who exists here, so this module answers exactly one question:
*which agent definitions can the current project delegate to, and what do they
claim to be good at?*

The roster is read from the filesystem rather than the ``agents`` table because
it must agree with what the runtime will actually load: ``agent_sync``
resolves a slug as ``{project_fs_path}/agents/{slug}.agent.md`` first and the
global ``agents/`` dir second, and the delegated turn re-resolves it the same
way. A DB row can outlive its definition file (or predate the lazy project
sync), which would advertise an agent whose turn then fails with
``agent_config_not_found``.

Scoping is the project's own ``agents/`` dir plus the framework-wide one —
never a sibling project. Project agents must carry the ``{project_slug}--``
prefix (the same rule ``agent_sync`` enforces) and the frontmatter ``slug``
must equal the file stem, so every slug we return resolves back to the file we
read it from.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Iterable

import frontmatter

from .definition_check import project_slug_from_workspace, validate_agent_slug

log = logging.getLogger("agents_universe.agent_core.delegation")

#: Definition file suffix for agents.
AGENT_SUFFIX = ".agent.md"


def _as_list(value: object) -> list[str]:
    """Normalize a frontmatter list field to a list of strings.

    LLM-written definitions often spell a list as a scalar
    (``delegates_to: "tech-lead"``); mirrors ``AgentConfig._list_field`` so a
    scalar never gets iterated per character.
    """
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


@dataclass(frozen=True)
class DelegationCandidate:
    """One agent a delegating agent may hand a subtask to."""

    slug: str
    display_name: str
    description: str
    category: str
    skills: tuple[str, ...]
    workflows: tuple[str, ...]
    definition_path: str

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable form (tool results are JSON-encoded)."""
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "skills": list(self.skills),
            "workflows": list(self.workflows),
            "definition_path": self.definition_path,
        }


def _load_candidate(path: Path, *, require_prefix: str | None) -> DelegationCandidate | None:
    """Parse one definition file, or None when it must not be advertised.

    Skipped (log line only, mirroring ``agent_sync.sync_agents_dir``): a file
    whose frontmatter does not parse, whose ``slug`` is missing/unsafe, whose
    slug does not match the file stem, or (in a project dir) whose slug lacks
    the ``{project_slug}--`` prefix.
    """
    try:
        post = frontmatter.load(str(path))
    except Exception:
        log.warning("Delegation roster: unparsable agent definition %s", path)
        return None

    slug = post.get("slug")
    if not slug or not validate_agent_slug(slug):
        log.warning("Delegation roster: %s has missing/unsafe slug %r", path, slug)
        return None
    if slug != path.name.removesuffix(AGENT_SUFFIX):
        # The runtime resolves `{slug}.agent.md`, so a mismatched stem would
        # advertise a slug that cannot be loaded.
        log.warning("Delegation roster: %s declares slug %r, skipped", path, slug)
        return None
    if require_prefix and not slug.startswith(require_prefix):
        log.warning(
            "Delegation roster: project agent %r in %s lacks required prefix %r, skipped",
            slug, path, require_prefix,
        )
        return None

    return DelegationCandidate(
        slug=slug,
        display_name=str(post.get("display_name") or slug),
        description=str(post.get("description") or ""),
        category=str(post.get("category") or "agile-development"),
        skills=tuple(_as_list(post.get("skills"))),
        workflows=tuple(_as_list(post.get("workflows"))),
        definition_path=str(path),
    )


def list_delegation_candidates(
    project_fs_path: str | None,
    framework_root: str | None,
    *,
    exclude: Iterable[str] = (),
    allow: Collection[str] | None = None,
) -> list[DelegationCandidate]:
    """Return the agents the current project may delegate to.

    Project definitions shadow framework ones of the same slug. ``exclude``
    drops slugs that must not be called (the caller itself, and any agent
    already running further up the delegation chain); ``allow``, when
    non-empty, restricts the result to that allowlist (an agent definition's
    optional ``delegates_to:`` frontmatter; ``None`` or empty means the whole
    roster).

    ``framework_root`` is the repo root holding the global ``agents/`` dir
    (``ToolContext.framework_root``). Passing ``None`` lists project agents
    only — the correct degraded behavior for an agent-core run outside the web
    service.
    """
    blocked = set(exclude)
    allowed = {a for a in allow} if allow else None

    found: dict[str, DelegationCandidate] = {}

    # Framework dir first so a project definition of the same slug wins.
    # (Project slugs are prefixed, so a genuine collision is unlikely — but the
    # shadowing rule must match resolve_agent_definition_path either way.)
    if framework_root:
        global_dir = Path(framework_root) / "agents"
        for md_file in sorted(global_dir.glob(f"*{AGENT_SUFFIX}")):
            candidate = _load_candidate(md_file, require_prefix=None)
            if candidate:
                found[candidate.slug] = candidate

    if project_fs_path:
        project_dir = Path(project_fs_path) / "agents"
        project_slug = project_slug_from_workspace(project_fs_path)
        prefix = f"{project_slug}--" if project_slug else None
        for md_file in sorted(project_dir.glob(f"*{AGENT_SUFFIX}")):
            candidate = _load_candidate(md_file, require_prefix=prefix)
            if candidate:
                found[candidate.slug] = candidate

    return sorted(
        (
            c
            for slug, c in found.items()
            if slug not in blocked and (allowed is None or slug in allowed)
        ),
        key=lambda c: (c.category, c.display_name, c.slug),
    )


def read_delegation_policy(definition_path: str | None) -> tuple[str, ...]:
    """Return the ``delegates_to`` allowlist declared by an agent definition.

    Empty tuple means "no restriction — the whole roster", which is also what
    an unreadable or missing file yields: a policy we cannot read must not be
    mistaken for a policy that forbids everything.
    """
    if not definition_path:
        return ()
    try:
        if not Path(definition_path).is_file():
            return ()
        post = frontmatter.load(definition_path)
    except Exception:
        log.warning("Delegation policy: unparsable definition %s", definition_path)
        return ()
    return tuple(_as_list(post.get("delegates_to")))
