"""knowledge_load_events persistence + turn-start rehydrate wiring.

The events table existed but was never written; loads evaporated at every
context rebuild. These tests pin the two halves of the fix:
1. knowledge_rw load/unload writes real event rows (SQLite here),
   last-event-per-slug wins when folded,
2. agent_turn passes conversation_id into load_project_context so the
   rehydrate path runs each turn.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from api.models.conversation import Conversation
from api.models.knowledge import KnowledgeLoadEvent, KnowledgeMetadata
from api.models.project import Project


async def _make_conversation(db) -> tuple[Project, Conversation]:
    project = Project(
        slug=f"p-{uuid.uuid4().hex[:8]}",
        display_name="load-events",
        created_by="test-user",
        visibility="public",
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    convo = Conversation(project_id=project.project_id, user_id="test-user")
    db.add(convo)
    await db.commit()
    await db.refresh(convo)
    return project, convo


def _detail_row(project_id: str, slug: str) -> KnowledgeMetadata:
    return KnowledgeMetadata(
        project_id=project_id,
        slug=slug,
        title="Users Service",
        category="technical",
        fs_path=f"/ws/knowledge/{slug}.md",
        knowledge_level="detail",
        parent_slug="technical/api-map",
        summary="users endpoints",
    )


# ── knowledge_rw writes events ──────────────────────────────────────────────


async def test_load_writes_event_and_unload_is_symmetric(db, tmp_path):
    from agent_core.knowledge.loader import KnowledgeContextResult, KnowledgeEntry
    from agent_core.tools.base import ToolContext
    from agent_core.tools.knowledge_rw import KnowledgeRWTool

    project, convo = await _make_conversation(db)
    row = _detail_row(project.project_id, "technical/api/users")
    db.add(row)
    await db.commit()
    await db.refresh(row)

    kdir = tmp_path / "knowledge"
    f = kdir / "technical" / "api" / "users.md"
    f.parent.mkdir(parents=True)
    f.write_text(
        "---\ntitle: Users\nknowledge_level: detail\nsummary: users endpoints\n---\nGET /users\n",
        encoding="utf-8",
    )

    ctx_result = KnowledgeContextResult()
    ctx_result.deferred_entries["technical/api/users"] = KnowledgeEntry(
        knowledge_id=str(row.knowledge_id), slug="technical/api/users",
        title="Users", fs_path=str(f), category="technical",
        cross_references=[], word_count=3, knowledge_level="detail",
        summary="users endpoints", project_id=project.project_id,
    )
    tool_ctx = ToolContext(
        project_id=project.project_id,
        project_fs_path=str(tmp_path),
        conversation_id=convo.conversation_id,
        user_id="test-user",
        db_session=db,
        project_context=ctx_result,
        current_turn=3,
    )

    tool = KnowledgeRWTool()
    result = await tool.execute(
        {"operation": "load", "slug": "technical/api/users"}, tool_ctx
    )
    assert result["status"] == "loaded"
    assert result["bytes"] > 0
    assert result["summary"] == "users endpoints"
    # Load must NOT bind to the current task — task binding is what made
    # plan_task pipelines unload the file at task end.
    assert "bound_to_task" not in result

    events = (await db.execute(
        select(KnowledgeLoadEvent).where(
            KnowledgeLoadEvent.conversation_id == convo.conversation_id
        )
    )).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == "load"
    assert events[0].reason == "agent_request"
    assert events[0].turn_number == 3
    assert events[0].knowledge_id == str(row.knowledge_id)

    # Unload writes the symmetric event.
    unload = await tool.execute(
        {"operation": "unload", "slug": "technical/api/users"}, tool_ctx
    )
    assert unload["status"] == "unloaded"
    events = (await db.execute(
        select(KnowledgeLoadEvent).where(
            KnowledgeLoadEvent.conversation_id == convo.conversation_id
        )
    )).scalars().all()
    assert [e.event_type for e in events] == ["load", "unload"]


async def test_load_without_metadata_row_skips_event(db, tmp_path):
    """FK guard: no knowledge_metadata row → no event, but the in-memory load
    still succeeds (the warning tells the agent to reindex)."""
    from agent_core.knowledge.loader import KnowledgeContextResult
    from agent_core.tools.base import ToolContext
    from agent_core.tools.knowledge_rw import KnowledgeRWTool

    project, convo = await _make_conversation(db)
    kdir = tmp_path / "knowledge"
    f = kdir / "domain" / "orphan.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\ntitle: Orphan\nknowledge_level: detail\n---\nbody\n", encoding="utf-8")

    ctx_result = KnowledgeContextResult()
    tool_ctx = ToolContext(
        project_id=project.project_id,
        project_fs_path=str(tmp_path),
        conversation_id=convo.conversation_id,
        user_id="test-user",
        db_session=db,
        project_context=ctx_result,
    )

    tool = KnowledgeRWTool()
    result = await tool.execute({"operation": "load", "slug": "domain/orphan"}, tool_ctx)
    assert result["status"] == "loaded"

    events = (await db.execute(
        select(KnowledgeLoadEvent).where(
            KnowledgeLoadEvent.conversation_id == convo.conversation_id
        )
    )).scalars().all()
    assert events == []  # skipped, no FK target


async def test_load_without_conversation_id_still_loads(db, tmp_path):
    """Headless contexts have no conversation — the load works, no event."""
    from agent_core.knowledge.loader import KnowledgeContextResult
    from agent_core.tools.base import ToolContext
    from agent_core.tools.knowledge_rw import KnowledgeRWTool

    project, _convo = await _make_conversation(db)
    row = _detail_row(project.project_id, "technical/api/x")
    db.add(row)

    kdir = tmp_path / "knowledge"
    f = kdir / "technical" / "api" / "x.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\ntitle: X\nknowledge_level: detail\n---\nbody\n", encoding="utf-8")

    ctx_result = KnowledgeContextResult()
    tool_ctx = ToolContext(
        project_id=project.project_id,
        project_fs_path=str(tmp_path),
        conversation_id="",  # headless
        user_id="test-user",
        db_session=db,
        project_context=ctx_result,
    )

    tool = KnowledgeRWTool()
    result = await tool.execute({"operation": "load", "slug": "technical/api/x"}, tool_ctx)
    assert result["status"] == "loaded"


# ── Event folding (the rehydrate query contract) ────────────────────────────


async def test_fold_last_event_per_slug_wins(db):
    """load → unload → load leaves the slug active; the fold in
    rehydrate_dynamic_entries must follow event order, not count."""
    project, convo = await _make_conversation(db)
    row = _detail_row(project.project_id, "technical/api/fold")
    db.add(row)
    await db.commit()
    await db.refresh(row)

    for i, event_type in enumerate(["load", "unload", "load"]):
        db.add(KnowledgeLoadEvent(
            knowledge_id=str(row.knowledge_id),
            conversation_id=convo.conversation_id,
            event_type=event_type,
            reason="test",
            turn_number=i,
        ))
    await db.commit()

    # Reproduce the fold rehydrate_dynamic_entries performs.
    from sqlalchemy import text

    rows = (await db.execute(
        text(
            "SELECT e.event_type, e.knowledge_id, m.slug "
            "FROM knowledge_load_events e "
            "JOIN knowledge_metadata m ON m.knowledge_id = e.knowledge_id "
            "WHERE e.conversation_id = :cid "
            "ORDER BY e.created_at ASC, e.turn_number ASC"
        ),
        {"cid": convo.conversation_id},
    )).mappings().all()

    active: dict[str, str] = {}
    for r in rows:
        if r["event_type"] == "load":
            active[r["slug"]] = r["event_type"]
        else:
            active.pop(r["slug"], None)
    assert active == {"technical/api/fold": "load"}

    # Append an unload → inactive.
    db.add(KnowledgeLoadEvent(
        knowledge_id=str(row.knowledge_id),
        conversation_id=convo.conversation_id,
        event_type="unload",
        reason="test",
        turn_number=99,
    ))
    await db.commit()
    rows = (await db.execute(
        text(
            "SELECT e.event_type, m.slug "
            "FROM knowledge_load_events e "
            "JOIN knowledge_metadata m ON m.knowledge_id = e.knowledge_id "
            "WHERE e.conversation_id = :cid "
            "ORDER BY e.created_at ASC, e.turn_number ASC"
        ),
        {"cid": convo.conversation_id},
    )).mappings().all()
    active = {}
    for r in rows:
        if r["event_type"] == "load":
            active[r["slug"]] = r["event_type"]
        else:
            active.pop(r["slug"], None)
    assert active == {}


async def test_events_are_conversation_scoped(db):
    project, convo_a = await _make_conversation(db)
    _p, convo_b = await _make_conversation(db)
    row = _detail_row(project.project_id, "technical/api/scope")
    db.add(row)
    await db.commit()
    await db.refresh(row)

    db.add(KnowledgeLoadEvent(
        knowledge_id=str(row.knowledge_id),
        conversation_id=convo_a.conversation_id,
        event_type="load", reason="test", turn_number=0,
    ))
    await db.commit()

    from sqlalchemy import text

    rows_b = (await db.execute(
        text(
            "SELECT e.event_type FROM knowledge_load_events e "
            "WHERE e.conversation_id = :cid"
        ),
        {"cid": convo_b.conversation_id},
    )).all()
    assert rows_b == []


# ── agent_turn wires conversation_id into load_project_context ──────────────


def test_agent_turn_passes_conversation_id_to_loader():
    """run_turn must hand conversation_id to load_project_context — without
    it the rehydrate path never runs and loads die at each turn boundary.

    A full run_turn needs agent rows, model config, and an LLM, so this pins
    the call site directly (the loader-side behavior is covered by
    agent-core's test_knowledge_rehydrate.py).
    """
    import inspect

    import api.services.agent_turn as agent_turn_mod

    src = inspect.getsource(agent_turn_mod)
    call_idx = src.find("project_context = await load_project_context(")
    assert call_idx != -1
    call_src = src[call_idx: call_idx + 800]
    assert "conversation_id=conversation_id" in call_src
