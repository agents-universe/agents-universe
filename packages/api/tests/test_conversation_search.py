"""Conversation keyword search (list_conversations?q=).

Covers: title vs message-body matching, project/user isolation, publish and
soft-deleted exclusion, cross-agent results, literal %/_ handling and the
dialect-specific T-SQL bracket escape.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from api.models.agent import Agent
from api.models.conversation import Conversation, Message as DbMessage
from api.routers.conversations import _like_needle


async def _make_conversation(
    db,
    project,
    *,
    title: str | None = None,
    user_id: str = "test-user",
    messages: tuple[str, ...] = (),
    source: str | None = None,
    status: str = "active",
    agent: Agent | None = None,
    updated_at: datetime | None = None,
) -> Conversation:
    conv = Conversation(
        conversation_id=f"c-{uuid.uuid4().hex[:8]}",
        project_id=project.project_id,
        user_id=user_id,
        title=title,
        source=source,
        status=status,
    )
    if agent is not None:
        conv.agent_id = agent.agent_id
    if updated_at is not None:
        conv.updated_at = updated_at
    db.add(conv)
    await db.commit()
    for i, content in enumerate(messages):
        db.add(DbMessage(
            conversation_id=str(conv.conversation_id),
            role="user" if i % 2 == 0 else "assistant",
            content=content,
            sequence_num=i + 1,
        ))
    if messages:
        await db.commit()
    return conv


async def _search(client, project, **params):
    resp = await client.get(
        f"/api/projects/{project.project_id}/conversations", params=params
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_search_matches_title_case_insensitive(client, db, make_project):
    project = await make_project("srch-title")
    await _make_conversation(db, project, title="Hello World")
    await _make_conversation(db, project, title="unrelated")

    rows = await _search(client, project, q="hello")
    assert [r["title"] for r in rows] == ["Hello World"]


async def test_search_matches_message_body(client, db, make_project):
    project = await make_project("srch-body")
    await _make_conversation(db, project, title="first", messages=("discuss KAFKA topics",))
    await _make_conversation(db, project, title="second", messages=("hello", "KAFKA again"))
    await _make_conversation(db, project, title="third", messages=("nothing here",))

    rows = await _search(client, project, q="kafka")
    assert {r["title"] for r in rows} == {"first", "second"}
    # One row per conversation even though "second" has two matching messages.
    assert len(rows) == 2


async def test_search_scoped_to_project(client, db, make_project):
    project_a = await make_project("srch-a")
    project_b = await make_project("srch-b")
    await _make_conversation(db, project_a, title="shared keyword A")
    await _make_conversation(db, project_b, title="shared keyword B")

    rows = await _search(client, project_a, q="shared keyword")
    assert [r["title"] for r in rows] == ["shared keyword A"]


async def test_search_scoped_to_user(client, db, make_project, as_user):
    project = await make_project("srch-user")
    await _make_conversation(db, project, title="mine unique", user_id="test-user")
    await _make_conversation(db, project, title="theirs unique", user_id="other-user")

    rows = await _search(client, project, q="unique")
    assert [r["title"] for r in rows] == ["mine unique"]

    async with as_user("other-user"):
        rows = await _search(client, project, q="unique")
    assert [r["title"] for r in rows] == ["theirs unique"]


async def test_search_excludes_publish_source(client, db, make_project):
    project = await make_project("srch-publish")
    await _make_conversation(db, project, title="public keyword")
    await _make_conversation(db, project, title="public keyword", source="publish")

    rows = await _search(client, project, q="public keyword")
    assert len(rows) == 1


async def test_search_excludes_soft_deleted(client, db, make_project):
    project = await make_project("srch-deleted")
    await _make_conversation(db, project, title="alive keyword")
    await _make_conversation(db, project, title="alive keyword", status="deleted")

    rows = await _search(client, project, q="alive keyword")
    assert len(rows) == 1


async def test_search_across_all_agents_including_agentless(client, db, make_project):
    project = await make_project("srch-agents")
    a1 = Agent(slug="srch-a1", display_name="A1", is_system=True)
    a2 = Agent(slug="srch-a2", display_name="A2", is_system=True)
    db.add_all([a1, a2])
    await db.commit()

    await _make_conversation(db, project, title="match a1", agent=a1)
    await _make_conversation(db, project, title="match a2", agent=a2)
    await _make_conversation(db, project, title="match none")

    rows = await _search(client, project, q="match")
    assert {r["agent_slug"] for r in rows} == {"srch-a1", "srch-a2", None}
    assert len(rows) == 3


async def test_search_with_agent_slug_is_intersection(client, db, make_project):
    project = await make_project("srch-intersect")
    a1 = Agent(slug="srch-b1", display_name="B1", is_system=True)
    a2 = Agent(slug="srch-b2", display_name="B2", is_system=True)
    db.add_all([a1, a2])
    await db.commit()

    await _make_conversation(db, project, title="hit b1", agent=a1)
    await _make_conversation(db, project, title="hit b2", agent=a2)

    rows = await _search(client, project, q="hit", agent_slug="srch-b1")
    assert [r["title"] for r in rows] == ["hit b1"]


async def test_search_escapes_like_wildcards(client, db, make_project):
    project = await make_project("srch-wildcard")
    await _make_conversation(db, project, title="100% done")
    await _make_conversation(db, project, title="1000 done")
    await _make_conversation(db, project, title="snake_case")
    await _make_conversation(db, project, title="snakeXcase")

    assert [r["title"] for r in await _search(client, project, q="100%")] == ["100% done"]
    assert [r["title"] for r in await _search(client, project, q="snake_case")] == ["snake_case"]


async def test_search_blank_q_is_ignored(client, db, make_project):
    project = await make_project("srch-blank")
    await _make_conversation(db, project, title="one")
    await _make_conversation(db, project, title="two")

    assert len(await _search(client, project, q="   ")) == 2


async def test_search_rejects_overlong_q(client, make_project):
    project = await make_project("srch-long")
    resp = await client.get(
        f"/api/projects/{project.project_id}/conversations", params={"q": "x" * 101}
    )
    assert resp.status_code == 422


async def test_search_preserves_order_and_shape(client, db, make_project):
    project = await make_project("srch-order")
    older = datetime.now(timezone.utc) - timedelta(days=2)
    newer = datetime.now(timezone.utc)
    await _make_conversation(db, project, title="order old", updated_at=older)
    await _make_conversation(db, project, title="order new", updated_at=newer)

    rows = await _search(client, project, q="order")
    assert [r["title"] for r in rows] == ["order new", "order old"]
    assert {"conversation_id", "agent_slug", "message_count", "tokens_used",
            "is_running", "created_at", "updated_at"} <= set(rows[0])


def test_like_needle_escapes_bracket_on_mssql_only():
    assert _like_needle("[Draft] 50%", "mssql") == "[[]draft] 50%"
    # % and _ are left to SQLAlchemy's autoescape; only [ is dialect-specific.
    assert _like_needle("[Draft] 50%", "postgresql") == "[draft] 50%"
    assert _like_needle("[Draft] 50%", "sqlite") == "[draft] 50%"
    assert _like_needle("[Draft] 50%", "mysql") == "[draft] 50%"
