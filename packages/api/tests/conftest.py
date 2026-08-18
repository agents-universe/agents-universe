"""Shared fixtures for API tests.

Env vars MUST be set before any ``api.*`` import (the engine/settings are
built at module import time), so they are assigned at module top here.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

_TMP = Path(__file__).parent / "_tmp"
_PROJECTS_ROOT = _TMP / "projects"
_DB_FILE = _TMP / "test.db"

# Force test env before importing api modules.
os.environ["AUTH_BYPASS_ENABLED"] = "true"
os.environ["AUTH_BYPASS_USER_ID"] = "test-user"
os.environ["PROJECTS_ROOT"] = str(_PROJECTS_ROOT)
# setdefault (not assignment) so a CI job can point the suite at PostgreSQL via
# TEST_DATABASE_URL — a hard assignment would silently defeat it.
os.environ.setdefault("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{_DB_FILE.as_posix()}")

# Now safe to import the app (engine + settings read the env above).
from api.database import AsyncSessionLocal, engine  # noqa: E402
from api.models.project import Project  # noqa: E402
from api.main import app  # noqa: E402

from alembic.config import Config  # noqa: E402

_ALEMBIC_INI = Path(__file__).parent.parent / "alembic.ini"
_ALEMBIC_CFG = Config(str(_ALEMBIC_INI))
_ALEMBIC_CFG.set_main_option("script_location", str(_ALEMBIC_INI.parent / "alembic"))


@pytest.fixture(scope="session", autouse=True)
def _fresh_schema():
    """Start each test session with a clean DB + clean projects root.

    The schema is built by running the real Alembic chain to head (not
    metadata.create_all) — this makes migration portability a tested contract:
    every dialect the suite runs against (SQLite locally, PostgreSQL in CI)
    must execute the full 25-migration history. Server DBs (PG/MySQL) are
    reset with a full downgrade to base first — a file DB just gets deleted.
    """
    if _DB_FILE.exists():
        _DB_FILE.unlink()
    if _PROJECTS_ROOT.exists():
        import shutil
        shutil.rmtree(_PROJECTS_ROOT)
    _PROJECTS_ROOT.mkdir(parents=True)

    from alembic import command
    if not os.environ.get("TEST_DATABASE_URL", "").startswith("sqlite"):
        # A migrated server DB keeps stale rows between runs; reset so every
        # run starts from an empty schema (downgrade is a no-op on a fresh DB).
        command.downgrade(_ALEMBIC_CFG, "base")
    command.upgrade(_ALEMBIC_CFG, "head")
    yield


@pytest.fixture(scope="session", autouse=True)
def _no_redis():
    """ASGITransport never runs the app lifespan, so Redis is unavailable.

    Auth bypass means get_current_user never touches the (unused) redis
    dependency — override it so FastAPI does not resolve the real one.
    """
    from api.services.redis_client import get_redis

    async def _fake_redis():
        yield None

    app.dependency_overrides[get_redis] = _fake_redis
    yield
    app.dependency_overrides.pop(get_redis, None)


@pytest.fixture(autouse=True)
async def _dispose_engine_per_test():
    """Keep pooled engines (asyncpg/aiomysql) loop-local.

    pytest-asyncio gives every test its own event loop; a connection pooled in
    test N's loop is poisoned for test N+1 ('Event loop is closed'). Must run
    INSIDE the test's loop: a sync teardown runs after the loop dies, and
    closing asyncpg connections is loop-bound. SQLite's NullPool makes this a
    no-op — required for the PG/MySQL CI runs.
    """
    yield
    await engine.dispose()


@pytest.fixture(scope="session")
def app_fixture():
    return app


@pytest.fixture
async def client(app_fixture):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session


@pytest.fixture
async def make_project(db):
    """Create a Project row + its workspace dirs; returns the Project."""

    async def _make(
        slug: str | None = None,
        *,
        created_by: str = "test-user",
        visibility: str = "public",
    ) -> Project:
        slug = slug or f"p-{uuid.uuid4().hex[:8]}"
        project = Project(
            slug=slug,
            display_name=slug,
            created_by=created_by,
            visibility=visibility,
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)
        ws = _PROJECTS_ROOT / slug
        (ws / "agents").mkdir(parents=True, exist_ok=True)
        (ws / "skills").mkdir(parents=True, exist_ok=True)
        (ws / "workflows").mkdir(parents=True, exist_ok=True)
        return project

    return _make


@pytest.fixture
def as_user():
    """Simulate another logged-in user via dependency override.

    Auth bypass env vars are read at import time, so per-test user switches
    must override get_current_user directly rather than mutating env.
    """
    from contextlib import asynccontextmanager

    from api.dependencies.auth import UserInfo, get_current_user

    @asynccontextmanager
    async def _as(user_id: str):
        app.dependency_overrides[get_current_user] = lambda: UserInfo(
            user_id=user_id, display_name=user_id
        )
        try:
            yield
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    return _as
