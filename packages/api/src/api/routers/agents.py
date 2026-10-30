"""Agents router — list available agents, sync project-scoped definitions."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.agent import Agent
from api.models.project import Project
from api.paths import resolve_project_fs_path
from api.services.agent_sync import PROJECT_SLUG_SEPARATOR, sync_agents_dir

router = APIRouter(prefix="/api/agents")


class SkillRef(BaseModel):
    slug: str
    description: str = ""
    public: bool = False
    #: Non-canonical locale variants of ``description`` (tooltip i18n).
    i18n: dict[str, str] | None = None


class AgentI18n(BaseModel):
    """Per-field locale maps for the non-canonical languages of an agent's
    UI text. The top-level AgentResponse fields stay the zh-first canonical;
    the frontend resolves ``overrides[locale] ?? canonical``."""

    display_name: dict[str, str] | None = None
    description: dict[str, str] | None = None
    placeholder: dict[str, str] | None = None
    starter_prompts: dict[str, list[str]] | None = None


class AgentResponse(BaseModel):
    agent_id: str
    slug: str
    display_name: str
    description: str | None
    category: str
    project_id: str | None = None
    skills: list[SkillRef]
    workflows: list[SkillRef]
    tools: list[str] = []
    placeholder: str | None = None
    starter_prompts: list[str] = []
    i18n: AgentI18n | None = None


class SyncResult(BaseModel):
    synced: list[str]
    removed: list[str]


def _split_scalar(raw: str) -> list[str]:
    """Split a scalar frontmatter value into its list items.

    Same defense as ``memory_rw._coerce_tags``: a bare string
    (`tools: "shell, filesystem"`) means multiple items, and a single slug is
    a one-item list. Slugs never contain commas, so comma-splitting is safe.
    """
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_refs(raw: str | None) -> list[SkillRef]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        items = _split_scalar(raw)
    if isinstance(items, str):
        # JSON stringified scalar: `_j` turned a scalar frontmatter value
        # into `"\"code-review\""`. Resolve it the same way the loader does
        # (comma-split) so the list endpoint matches the runtime.
        return [SkillRef(slug=s) for s in _split_scalar(items)]
    result = []
    for item in items if isinstance(items, (list, tuple)) else []:
        if isinstance(item, str):
            result.append(SkillRef(slug=item))
        elif isinstance(item, dict):
            result.append(SkillRef(slug=item.get("slug", ""), description=item.get("description", "")))
    return result


def _registry_refs(registry) -> dict[str, SkillRef]:
    """Project the startup skill/workflow registry into tooltip refs by slug.

    Only the base registry is scanned — the per-turn project overlay lives in
    ``agent_turn`` and does not apply at list time. The ``public`` flag
    attaches to the slug, so a project skill shadowing the same slug still
    shows as public (matching runtime behavior). ``registry`` is ``None`` when
    the app lifespan never ran (``ASGITransport`` in tests), which yields no
    injection.
    """
    if registry is None:
        return {}
    return {
        s.slug: SkillRef(
            slug=s.slug,
            description=s.description,
            public=s.public,
            i18n=s.description_i18n or None,
        )
        for s in registry.all()
    }


def _merge_public_refs(refs: list[SkillRef], public_refs: list[SkillRef]) -> list[SkillRef]:
    """Ensure every public skill/workflow appears exactly once in an agent's list.

    A matching entry may come from a stale DB row or a project agent whose
    frontmatter still lists the slug — keep it and just flag it public instead
    of appending a duplicate. Appended entries are copies: the registry refs
    are shared across every agent in the response.
    """
    by_slug = {r.slug: r for r in refs}
    for p in public_refs:
        existing = by_slug.get(p.slug)
        if existing is None:
            refs.append(p.model_copy())
            by_slug[p.slug] = refs[-1]
        else:
            existing.public = True
            if not existing.description:
                existing.description = p.description
            if existing.i18n is None:
                existing.i18n = p.i18n
    return refs


def _backfill_refs(
    refs: list[SkillRef], registry_map: dict[str, SkillRef], *, propagate_public: bool
) -> None:
    """Fill tooltip descriptions from the startup registry.

    Agent frontmatter lists skills/workflows by slug, so the stored JSON
    carries no description — without this backfill the tooltip shows nothing
    for non-public skills and for every workflow. The stored entry keeps
    precedence for its own description; only empties are filled.
    """
    for ref in refs:
        source = registry_map.get(ref.slug)
        if source is None:
            continue
        if not ref.description:
            ref.description = source.description
        if ref.i18n is None and source.i18n is not None:
            ref.i18n = source.i18n
        if propagate_public and source.public:
            ref.public = True


def _parse_agent_i18n(raw: str | None) -> AgentI18n | None:
    """Deserialize the agents.i18n JSON column; None on absence or garbage."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not data:
            return None
        model = AgentI18n.model_validate(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return model if model.model_dump(exclude_none=True) else None


def _parse_tool_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _split_scalar(raw)
    if isinstance(items, str):
        # JSON stringified scalar — comma-separated, same as the loader.
        return _split_scalar(items)
    return [str(t) for t in items if isinstance(t, str)] if isinstance(items, list) else []


def _parse_str_list(raw: str | None) -> list[str]:
    """Parse a JSON string array (starter prompts).

    Unlike ``_parse_tool_list`` this never comma-splits: prompts are free
    text and legitimately contain commas. Malformed JSON yields [].
    """
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(items, str):
        return [items]
    if isinstance(items, list):
        return [x for x in items if isinstance(x, str)]
    return []


def _to_response(
    a: Agent,
    public_refs: list[SkillRef] | None = None,
    skill_map: dict[str, SkillRef] | None = None,
    workflow_map: dict[str, SkillRef] | None = None,
    public_workflows: list[SkillRef] | None = None,
) -> AgentResponse:
    skills = _merge_public_refs(_parse_refs(a.skills), public_refs or [])
    _backfill_refs(skills, skill_map or {}, propagate_public=True)
    workflows = _merge_public_refs(_parse_refs(a.workflows), public_workflows or [])
    _backfill_refs(workflows, workflow_map or {}, propagate_public=True)
    return AgentResponse(
        agent_id=a.agent_id,
        slug=a.slug,
        display_name=a.display_name,
        description=a.description,
        category=a.category or "agile-development",
        project_id=a.project_id,
        skills=skills,
        workflows=workflows,
        tools=_parse_tool_list(a.tools),
        placeholder=a.placeholder,
        starter_prompts=_parse_str_list(a.starter_prompts),
        i18n=_parse_agent_i18n(a.i18n),
    )


async def _sync_project_agents(
    db: AsyncSession, project: Project
) -> tuple[list[str], list[str]]:
    """Lazily sync a project's agent definitions from its workspace.

    Runs before listing so the customization expert's file writes take effect
    without a restart. Returns (synced, removed) slug lists.
    """
    fs_path = await resolve_project_fs_path(project.project_id, db)
    return await sync_agents_dir(
        db,
        Path(fs_path) / "agents",
        project_id=project.project_id,
        is_system=False,
        slug_prefix=f"{project.slug}{PROJECT_SLUG_SEPARATOR}",
    )


@router.get("", response_model=list[AgentResponse])
async def list_agents(
    request: Request,
    project_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: UserInfo = Depends(get_current_user),
):
    """List global agents, plus project agents when ``project_id`` is given."""
    # getattr guard: tests run under ASGITransport without lifespan, so
    # app.state registries are absent there and nothing injects.
    skill_map = _registry_refs(getattr(request.app.state, "skill_registry", None))
    workflow_map = _registry_refs(getattr(request.app.state, "workflow_registry", None))
    public_refs = [r for r in skill_map.values() if r.public]
    public_workflow_refs = [r for r in workflow_map.values() if r.public]
    if project_id:
        project = await authorize_project(project_id, db, _current_user)
        await _sync_project_agents(db, project)
        result = await db.execute(
            select(Agent)
            .where(or_(Agent.project_id == project_id, Agent.project_id.is_(None)))
            .order_by(Agent.display_name)
        )
    else:
        result = await db.execute(
            select(Agent).where(Agent.project_id.is_(None)).order_by(Agent.display_name)
        )
    return [
        _to_response(a, public_refs, skill_map, workflow_map, public_workflow_refs)
        for a in result.scalars().all()
    ]


@router.post("/sync", response_model=SyncResult)
async def sync_project_agents(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: UserInfo = Depends(get_current_user),
):
    """Explicitly re-sync a project's agent definitions from its workspace."""
    project = await authorize_project(project_id, db, _current_user)
    synced, removed = await _sync_project_agents(db, project)
    return SyncResult(synced=synced, removed=removed)
