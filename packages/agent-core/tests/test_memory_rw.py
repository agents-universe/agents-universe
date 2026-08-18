"""Tests for memory_rw recall_episodes (SQL limit, project scoping)."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from agent_core.tools.base import ToolContext
from agent_core.tools.memory_rw import MemoryRWTool

_EPISODES_DDL = """
CREATE TABLE episodic_memories (
    episode_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    user_id TEXT,
    project_id TEXT,
    summary TEXT,
    key_findings TEXT,
    open_questions TEXT,
    created_at TEXT
)
"""


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_EPISODES_DDL)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _context(db_session) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=".",
        conversation_id="c1",
        user_id="u1",
        db_session=db_session,
    )


async def _seed(session: AsyncSession, n: int) -> None:
    from sqlalchemy import text

    for i in range(n):
        await session.execute(
            text(
                "INSERT INTO episodic_memories "
                "(episode_id, conversation_id, user_id, project_id, summary, "
                "key_findings, open_questions, created_at) "
                "VALUES (:eid, :cid, :uid, :pid, :sum, :kf, :oq, :ts)"
            ),
            {
                "eid": f"e{i}",
                "cid": f"c{i}",
                "uid": "u1",
                "pid": "p1",
                "sum": f"summary {i}",
                "kf": "[]",
                "oq": "[]",
                "ts": f"2026-01-{i + 1:02d}T00:00:00",
            },
        )
    await session.commit()


@pytest.mark.asyncio
async def test_recall_episodes_pushes_limit_into_sql(db_session):
    """The limit must reach the SQL (LIMIT on SQLite), not a post-fetchall
    Python slice — 30 rows with limit 5 must return exactly 5 newest rows."""
    await _seed(db_session, 30)
    tool = MemoryRWTool()
    ctx = _context(db_session)

    result = await tool.execute({"operation": "recall_episodes", "limit": 5}, ctx)

    assert "error" not in result
    episodes = result["episodes"]
    assert len(episodes) == 5
    # Newest first (created_at DESC): the 5 highest seed ids.
    assert [e["episode_id"] for e in episodes] == ["e29", "e28", "e27", "e26", "e25"]


@pytest.mark.asyncio
async def test_recall_episodes_uses_fetch_clause_on_mssql():
    """The mssql dialect branch must compile OFFSET/FETCH (no LIMIT) and pass
    :lim as a bound parameter."""
    captured: dict = {}

    class FakeBind:
        dialect = type("Dialect", (), {"name": "mssql"})()

    class FakeSession:
        bind = FakeBind()

        async def execute(self, stmt, params):
            captured["sql"] = str(stmt)
            captured["params"] = params

            class _Row:
                pass

            class _Result:
                def fetchall(self):
                    return []

            return _Result()

    ctx = ToolContext(
        project_id="p1", project_fs_path=".", conversation_id="c1", user_id="u1",
        db_session=FakeSession(),
    )
    tool = MemoryRWTool()
    result = await tool.execute({"operation": "recall_episodes", "limit": 7}, ctx)

    assert result == {"episodes": []}
    assert "FETCH NEXT :lim ROWS ONLY" in captured["sql"]
    assert "LIMIT" not in captured["sql"]
    assert captured["params"]["lim"] == 7
