"""Project-scoped agent definition sync.

Agent definitions are files (``*.agent.md``); the ``agents`` table is a
projection used for listing and isolation checks. Global agents live in the
framework ``agents/`` dir and are synced at startup; project agents live in
``{PROJECTS_ROOT}/{slug}/agents/`` and are synced lazily (on list/sync API
calls) so the customization expert's file writes take effect without a restart.

Slug rules:
- All slugs must match ``[a-z0-9][a-z0-9-]*`` (path-safe, no ``..`` or ``/``).
- Project agents MUST use the ``{project_slug}--{name}`` prefix so they can be
  told apart from global slugs (which never contain ``--``).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import frontmatter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.agent import Agent
from api.models.conversation import Conversation

log = logging.getLogger("agents_universe.agent_sync")

#: Separator between project slug and agent name in project agent slugs.
PROJECT_SLUG_SEPARATOR = "--"

#: Path-safe slug: lowercase letters, digits, dashes; must start alnum.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_agent_slug(slug: str) -> bool:
    """Return True when the slug is safe to use as a filename segment."""
    return bool(_SLUG_RE.match(slug))


def resolve_agent_definition_path(slug: str, project_fs_path: str | None) -> str | None:
    """Resolve an agent definition file for a slug.

    The project workspace ``agents/`` dir shadows the global framework dir:
    the project version wins for the same slug. Returns None when no
    definition exists. Raises ValueError for unsafe slugs.
    """
    from api.paths import AGENTS_DIR

    if not validate_agent_slug(slug):
        raise ValueError(f"Unsafe agent slug: {slug!r}")

    if project_fs_path:
        project_path = Path(project_fs_path) / "agents" / f"{slug}.agent.md"
        if project_path.is_file():
            return str(project_path)

    global_path = AGENTS_DIR / f"{slug}.agent.md"
    if global_path.is_file():
        return str(global_path)
    return None


async def sync_agents_dir(
    session: AsyncSession,
    agents_dir: str | Path,
    *,
    project_id: str | None = None,
    is_system: bool = False,
    slug_prefix: str | None = None,
) -> tuple[list[str], list[str]]:
    """Upsert agent definitions from a directory of ``*.agent.md`` files.

    Scope semantics (mirroring the startup sync):
    - ``project_id is None`` (global): rows with ``is_system=True`` are
      managed; the agents dir is expected to exist.
    - ``project_id`` set (project): rows with ``project_id == <pid>`` are
      managed; a missing dir simply means no definitions, so all such rows are
      cleaned up (correct cascade when a project workspace is removed).

    Rows whose definition file disappeared are deleted after NULLing the
    ``conversations.agent_id`` references. Returns ``(synced, removed)`` slug
    lists.
    """
    path = Path(agents_dir)
    synced: list[str] = []

    if path.exists():
        for md_file in sorted(path.glob("*.agent.md")):
            try:
                post = frontmatter.load(str(md_file))
            except Exception:
                log.exception("Failed to parse agent definition %s", md_file)
                continue

            slug = post.get("slug")
            if not slug:
                log.warning("Agent definition %s has no slug, skipped", md_file)
                continue
            if not validate_agent_slug(slug):
                log.warning("Agent definition %s has unsafe slug %r, skipped", md_file, slug)
                continue
            if slug_prefix and not slug.startswith(slug_prefix):
                log.warning(
                    "Project agent slug %r in %s lacks required prefix %r, skipped",
                    slug, md_file, slug_prefix,
                )
                continue

            def _j(v):
                return json.dumps(v) if v else None

            attrs = dict(
                display_name=post.get("display_name", slug),
                description=post.get("description"),
                category=post.get("category") or "agile-development",
                definition_path=str(md_file),
                tools=_j(post.get("tools", [])),
                skills=_j(post.get("skills", [])),
                workflows=_j(post.get("workflows", [])),
            )

            try:
                result = await session.execute(select(Agent).where(Agent.slug == slug))
                agent = result.scalar_one_or_none()
                if agent is None:
                    agent = Agent(slug=slug, project_id=project_id, is_system=is_system, **attrs)
                    session.add(agent)
                    log.info(
                        "Registered %s agent: %s",
                        "project" if project_id else "system",
                        slug,
                    )
                else:
                    # A slug already claimed by another scope must not be hijacked.
                    if (agent.project_id or None) != project_id or agent.is_system != is_system:
                        log.warning(
                            "Agent slug %r already exists in another scope (%s), skipped",
                            slug, "project " + agent.project_id if agent.project_id else "global",
                        )
                        continue
                    for k, v in attrs.items():
                        setattr(agent, k, v)
                synced.append(slug)
            except Exception:
                log.exception("Failed to sync agent %s", md_file)
    elif project_id is None:
        # Global dir is expected to exist; nothing to manage if it does not.
        return [], []

    # Remove managed rows whose definition file no longer exists. Matching on
    # definition_path (not "not in synced") preserves rows whose file failed
    # to parse this run — dropping them would NULL conversations.agent_id for
    # a file that is still on disk.
    scope_q = select(Agent).where(
        Agent.project_id == project_id if project_id is not None else Agent.is_system == True  # noqa: E712
    )
    if path.exists():
        existing_files = {str(f) for f in path.glob("*.agent.md")}
        if existing_files:
            scope_q = scope_q.where(Agent.definition_path.notin_(existing_files))
        # Dir exists but is empty: no definition files at all, so every
        # managed row in this scope is stale. (NOT is_(None) — synced rows
        # always carry a definition_path, so that filter would match nothing.)
    stale = (await session.execute(scope_q)).scalars().all()

    removed: list[str] = []
    for stale_agent in stale:
        await session.execute(
            update(Conversation)
            .where(Conversation.agent_id == stale_agent.agent_id)
            .values(agent_id=None)
        )
        await session.delete(stale_agent)
        removed.append(stale_agent.slug)
        log.info("Removed stale %s agent: %s", "project" if project_id else "system", stale_agent.slug)

    try:
        await session.commit()
    except Exception:
        # A bad row (e.g. column over length) must not 500 the whole agents
        # endpoint; roll back and report what was attempted.
        await session.rollback()
        log.exception("Failed to commit agent sync for %s", agents_dir)
    return synced, removed
