"""Usage dashboard router: aggregation semantics, windowing, scoping, auth."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.main import app
from api.models.conversation import Conversation
from api.models.conversation_run import ConversationRun


async def _make_conversation(
    db, project_id: str, user_id: str = "test-user", *, status: str = "active"
) -> Conversation:
    conv = Conversation(project_id=project_id, user_id=user_id, status=status)
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _seed_run(
    db,
    conversation: Conversation,
    *,
    started_at: datetime,
    tokens_used: int | None,
    status: str = "completed",
) -> ConversationRun:
    run = ConversationRun(
        conversation_id=str(conversation.conversation_id),
        status=status,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=1),
        tokens_used=tokens_used,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _days_ago(days: int, *, hour: int = 12) -> datetime:
    return datetime.now(timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0
    ) - timedelta(days=days)


class _FakeNameRedis:
    """Minimal hash client backing the user_display_names mirror."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def hset(self, key: str, field: str, value: str) -> None:
        self.store[field] = value

    async def hmget(self, key: str, fields: list[str]) -> list[str | None]:
        return [self.store.get(f) for f in fields]


class TestTopUserDisplayName:
    async def test_display_name_resolved_and_falls_back_to_id(
        self, client, db, make_project
    ):
        from api.main import app
        from api.services.redis_client import get_redis

        project = await make_project()
        conv = await _make_conversation(db, str(project.project_id), "alice-id")
        await _seed_run(db, conv, started_at=_days_ago(1), tokens_used=500)

        fake = _FakeNameRedis()

        async def _fake_redis():
            yield fake

        app.dependency_overrides[get_redis] = _fake_redis
        try:
            # Mirror absent → null, frontend falls back to the raw id.
            resp = await client.get(f"/api/projects/{project.project_id}/usage")
            assert resp.status_code == 200, resp.text
            assert resp.json()["top_users"][0]["display_name"] is None

            # After the login/request mirror writes the name, it is served.
            await fake.hset("user_display_names", "alice-id", "Alice Zhang")
            resp = await client.get(f"/api/projects/{project.project_id}/usage")
            entry = resp.json()["top_users"][0]
            assert entry["user_id"] == "alice-id"
            assert entry["display_name"] == "Alice Zhang"
        finally:
            # Restore conftest's None-yielding override (never pop: popping
            # would make later tests resolve the real, uninitialized pool).
            async def _no_redis():
                yield None

            app.dependency_overrides[get_redis] = _no_redis

    async def test_redis_outage_degrades_to_ids(self, client, db, make_project):
        from api.main import app
        from api.services.redis_client import get_redis

        project = await make_project()
        conv = await _make_conversation(db, str(project.project_id), "alice-id")
        await _seed_run(db, conv, started_at=_days_ago(1), tokens_used=500)

        class _DeadRedis:
            async def hmget(self, *args, **kwargs):
                raise TimeoutError("redis down")

        async def _dead_redis():
            yield _DeadRedis()

        app.dependency_overrides[get_redis] = _dead_redis
        try:
            resp = await client.get(f"/api/projects/{project.project_id}/usage")
            assert resp.status_code == 200, resp.text
            entry = resp.json()["top_users"][0]
            assert entry["display_name"] is None
            assert entry["user_id"] == "alice-id"
        finally:
            async def _no_redis():
                yield None

            app.dependency_overrides[get_redis] = _no_redis


class TestUsagePayload:
    async def test_empty_project(self, client, make_project):
        project = await make_project()
        resp = await client.get(f"/api/projects/{project.project_id}/usage")
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["days"] == 30
        assert data["summary"] == {
            "total_tokens": 0,
            "run_count": 0,
            "user_count": 0,
            "conversation_count": 0,
        }
        assert data["top_users"] == []

        # daily is zero-filled, ascending, exactly `days` entries ending today (UTC).
        daily = data["daily"]
        assert len(daily) == 30
        dates = [entry["date"] for entry in daily]
        assert dates == sorted(dates)
        today = datetime.now(timezone.utc).date()
        assert dates[-1] == today.isoformat()
        assert all(entry["tokens"] == 0 for entry in daily)

    async def test_top10_ordering_and_limit(self, client, db, make_project):
        project = await make_project()
        # 12 users, strictly decreasing tokens: user-12 has the most.
        for i in range(1, 13):
            conv = await _make_conversation(db, str(project.project_id), f"user-{i:02d}")
            await _seed_run(
                db, conv, started_at=_days_ago(1), tokens_used=i * 1000
            )

        resp = await client.get(f"/api/projects/{project.project_id}/usage")
        assert resp.status_code == 200, resp.text
        top = resp.json()["top_users"]

        assert len(top) == 10
        tokens = [entry["tokens"] for entry in top]
        assert tokens == sorted(tokens, reverse=True)
        assert [entry["rank"] for entry in top] == list(range(1, 11))
        assert top[0]["user_id"] == "user-12"
        assert top[0]["tokens"] == 12000
        # user-01 (1000) and user-02 (2000) fall out of the top 10.
        ids = {entry["user_id"] for entry in top}
        assert "user-01" not in ids
        assert "user-02" not in ids

    async def test_tie_break_is_deterministic(self, client, db, make_project):
        project = await make_project()
        # Equal tokens: more runs wins. Then equal runs: user_id ascending.
        conv_a = await _make_conversation(db, str(project.project_id), "tie-b")
        await _seed_run(db, conv_a, started_at=_days_ago(2), tokens_used=500)
        await _seed_run(db, conv_a, started_at=_days_ago(1), tokens_used=0)
        conv_b = await _make_conversation(db, str(project.project_id), "tie-a")
        await _seed_run(db, conv_b, started_at=_days_ago(1), tokens_used=500)
        conv_c = await _make_conversation(db, str(project.project_id), "tie-c")
        await _seed_run(db, conv_c, started_at=_days_ago(1), tokens_used=500)

        resp = await client.get(f"/api/projects/{project.project_id}/usage")
        top = resp.json()["top_users"]
        # tie-b: 500 tokens / 2 runs beats both 500/1; then tie-a < tie-c.
        assert [(e["user_id"], e["run_count"]) for e in top] == [
            ("tie-b", 2),
            ("tie-a", 1),
            ("tie-c", 1),
        ]

    async def test_window_filters_but_chain_crosses_boundary(
        self, client, db, make_project
    ):
        # Pre-window cumulative baseline (40 days ago) + in-window top-up
        # (5 days ago). The window must count only the delta (500), never
        # the whole lifetime ledger (1500), and the pre-window run itself
        # must not appear in run_count.
        project = await make_project()
        conv = await _make_conversation(db, str(project.project_id))
        await _seed_run(db, conv, started_at=_days_ago(40), tokens_used=1000)
        await _seed_run(db, conv, started_at=_days_ago(5), tokens_used=1500)

        resp = await client.get(f"/api/projects/{project.project_id}/usage?days=30")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["summary"]["total_tokens"] == 500
        assert data["summary"]["run_count"] == 1
        assert data["top_users"][0]["tokens"] == 500

    async def test_failed_zero_run_clamps_without_negatives(
        self, client, db, make_project
    ):
        # A failed turn persists tokens_used=0; running-max yields
        # [1000, 0, 200] — never a negative delta poisoning the chain.
        project = await make_project()
        conv = await _make_conversation(db, str(project.project_id))
        await _seed_run(db, conv, started_at=_days_ago(3), tokens_used=1000)
        await _seed_run(db, conv, started_at=_days_ago(2), tokens_used=0, status="failed")
        await _seed_run(db, conv, started_at=_days_ago(1), tokens_used=1200)

        resp = await client.get(f"/api/projects/{project.project_id}/usage")
        data = resp.json()
        assert data["summary"]["total_tokens"] == 1200
        assert data["summary"]["run_count"] == 3

    async def test_null_tokens_contribute_zero_and_keep_chain(
        self, client, db, make_project
    ):
        project = await make_project()
        conv = await _make_conversation(db, str(project.project_id))
        await _seed_run(db, conv, started_at=_days_ago(3), tokens_used=1000)
        await _seed_run(db, conv, started_at=_days_ago(2), tokens_used=None)
        await _seed_run(db, conv, started_at=_days_ago(1), tokens_used=1200)

        resp = await client.get(f"/api/projects/{project.project_id}/usage")
        data = resp.json()
        assert data["summary"]["total_tokens"] == 1200
        assert data["summary"]["run_count"] == 3

    async def test_soft_deleted_conversation_still_counted(
        self, client, db, make_project
    ):
        # Tokens were genuinely consumed; deletion of the sidebar row must
        # not erase them from the ledger view.
        project = await make_project()
        conv = await _make_conversation(
            db, str(project.project_id), status="deleted"
        )
        await _seed_run(db, conv, started_at=_days_ago(1), tokens_used=700)

        resp = await client.get(f"/api/projects/{project.project_id}/usage")
        data = resp.json()
        assert data["summary"]["total_tokens"] == 700
        assert data["top_users"][0]["user_id"] == "test-user"

    async def test_project_scoping(self, client, db, make_project):
        project_a = await make_project()
        project_b = await make_project()
        conv_a = await _make_conversation(db, str(project_a.project_id), "alice")
        await _seed_run(db, conv_a, started_at=_days_ago(1), tokens_used=100)
        conv_b = await _make_conversation(db, str(project_b.project_id), "bob")
        await _seed_run(db, conv_b, started_at=_days_ago(1), tokens_used=999999)

        resp = await client.get(f"/api/projects/{project_a.project_id}/usage")
        data = resp.json()
        assert data["summary"]["total_tokens"] == 100
        assert [e["user_id"] for e in data["top_users"]] == ["alice"]


class TestUsageAuth:
    async def test_private_project_denied_for_non_member(
        self, client, db, make_project, as_user
    ):
        project = await make_project(created_by="test-user")
        patch = await client.patch(
            f"/api/projects/{project.project_id}",
            json={"visibility": "private"},
        )
        assert patch.status_code == 200, patch.text

        async with as_user("other-user"):
            resp = await client.get(f"/api/projects/{project.project_id}/usage")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "PROJECT_PRIVATE"

    async def test_unknown_project_is_404(self, client):
        resp = await client.get(
            "/api/projects/00000000-0000-0000-0000-000000000000/usage"
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "PROJECT_NOT_FOUND"

    async def test_unauthenticated_is_401(self, client, make_project):
        from api.dependencies.auth import get_current_user
        from api.errors import api_error

        project = await make_project()

        async def _deny():
            raise api_error(401, "UNAUTHORIZED", "Not authenticated")

        app.dependency_overrides[get_current_user] = _deny
        try:
            resp = await client.get(f"/api/projects/{project.project_id}/usage")
        finally:
            app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 401

    async def test_days_out_of_range_is_422(self, client, make_project):
        project = await make_project()
        for days in (0, 91):
            resp = await client.get(
                f"/api/projects/{project.project_id}/usage?days={days}"
            )
            assert resp.status_code == 422, (days, resp.text)
