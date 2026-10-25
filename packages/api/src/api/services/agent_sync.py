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
from pathlib import Path

import frontmatter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

# Canonical slug rules live in agent-core, shared with the filesystem tool's
# write-time definition check — two copies would drift apart and let a file
# pass one gate while failing the other. Re-exported here because the routers
# and tests import them from this module.
from agent_core.definition_check import (
    PROJECT_SLUG_SEPARATOR as PROJECT_SLUG_SEPARATOR,  # re-exported for routers
    validate_agent_slug,
)
from agent_core.localization import normalize_i18n_field, normalize_i18n_prompts

from api.models.agent import Agent
from api.models.conversation import Conversation

log = logging.getLogger("agents_universe.agent_sync")

#: Canonical language order for agent UI fields — zh first, matching the
#: pre-i18n content (Chinese descriptions drive the display_name sort key and
#: the ``[display_name]:`` history prefix).
_ZH_FIRST = ("zh-CN", "en-US")


def _truncate(value: object, limit: int) -> str | None:
    """Coerce a frontmatter scalar to fit its column."""
    if value is None:
        return None
    return str(value)[:limit]


def _bounded_json(value: object, limit: int) -> str | None:
    """Serialize a list field, dropping trailing items until it fits *limit*.

    Frontmatter is user-editable and unbounded; one over-long list would
    otherwise fail the whole sync commit (SQL Server raises, SQLite silently
    ignores the width) and roll back every other agent in the directory.
    """
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        items = list(value)
        while items:
            encoded = json.dumps(items, ensure_ascii=False)
            if len(encoded) <= limit:
                return encoded
            items.pop()
        return None
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) <= limit:
        return encoded
    # Scalar: trim the string body until its JSON encoding fits.
    text = str(value)
    while text and len(json.dumps(text, ensure_ascii=False)) > limit:
        text = text[:-1]
    return json.dumps(text, ensure_ascii=False) if text else None


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
            # agents.slug / agents.definition_path widths. Truncating either
            # would corrupt identity or stale-row matching, so skip instead.
            if len(slug) > 100:
                log.warning("Agent definition %s has an over-long slug, skipped", md_file)
                continue
            if len(str(md_file)) > 500:
                log.warning("Agent definition path %s exceeds the column width, skipped", md_file)
                continue
            if slug_prefix and not slug.startswith(slug_prefix):
                log.warning(
                    "Project agent slug %r in %s lacks required prefix %r, skipped",
                    slug, md_file, slug_prefix,
                )
                continue

            # UI-facing fields accept a shared scalar or a {locale: value}
            # map — normalize BOTH shapes before the column-width truncation
            # below (a raw dict would str() into the canonical columns).
            # zh-first canonical keeps the pre-i18n Chinese text as the sort
            # key / history-prefix value; the full maps ride in the i18n JSON
            # column, each locale value bounded by the SAME widths so a map
            # cannot bypass the truncation guards.
            display_name, display_i18n = normalize_i18n_field(
                post.get("display_name"), canonical_order=_ZH_FIRST
            )
            description, description_i18n = normalize_i18n_field(
                post.get("description"), canonical_order=_ZH_FIRST
            )
            placeholder, placeholder_i18n = normalize_i18n_field(
                post.get("placeholder"), canonical_order=_ZH_FIRST
            )
            # New-conversation guidance: single-line composer placeholder and
            # a list of example prompts. A bare string is ONE prompt, never
            # comma-split (prompts contain commas); a locale map carries one
            # list per language. Empty list → None, matching the
            # tools/skills/workflows NULL convention.
            prompts, prompts_i18n = normalize_i18n_prompts(
                post.get("starter_prompts"), canonical_order=_ZH_FIRST
            )

            i18n_payload: dict[str, object] = {}
            if display_i18n:
                i18n_payload["display_name"] = {loc: v[:255] for loc, v in display_i18n.items()}
            if description_i18n:
                i18n_payload["description"] = {loc: v[:2000] for loc, v in description_i18n.items()}
            if placeholder_i18n:
                i18n_payload["placeholder"] = {loc: v[:500] for loc, v in placeholder_i18n.items()}
            if prompts_i18n:
                i18n_payload["starter_prompts"] = {
                    loc: items[:10] for loc, items in prompts_i18n.items()
                }

            attrs = dict(
                display_name=_truncate(display_name or slug, 255),
                description=_truncate(description, 2000),
                category=_truncate(
                    post.get("category") or "agile-development", 100
                ),
                definition_path=str(md_file),
                tools=_bounded_json(post.get("tools", []), 1000),
                skills=_bounded_json(post.get("skills", []), 2000),
                workflows=_bounded_json(post.get("workflows", []), 2000),
                placeholder=_truncate(placeholder, 500) if placeholder is not None else None,
                starter_prompts=json.dumps(prompts[:10], ensure_ascii=False) if prompts else None,
                i18n=json.dumps(i18n_payload, ensure_ascii=False) if i18n_payload else None,
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
        # endpoint; roll back and report nothing as synced — the rollback
        # discarded every insert/update/delete attempted above.
        await session.rollback()
        log.exception(
            "Failed to commit agent sync for %s (%d synced, %d removed rolled back)",
            agents_dir, len(synced), len(removed),
        )
        return [], []
    return synced, removed
