"""Scheduled-task router: CRUD, validation matrix, authorization, run-now."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from api.models.conversation import Conversation
from api.models.schedule import ScheduledTask, ScheduledTaskRun
from api.models.script import AutomationScript


async def _make_script(db, project_id: str, *, script_type: str = "python") -> AutomationScript:
    script = AutomationScript(
        project_id=project_id,
        name="nightly",
        script_type=script_type,
        content="print('hi')",
    )
    db.add(script)
    await db.commit()
    await db.refresh(script)
    return script


async def _make_conversation(db, project_id: str, user_id: str = "test-user") -> Conversation:
    conv = Conversation(project_id=project_id, user_id=user_id, title="target")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


def _body(project, **overrides) -> dict:
    body = {
        "name": "nightly",
        "target_type": "script",
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Shanghai",
    }
    body.update(overrides)
    return body


class TestCreate:
    async def test_creates_script_task_with_next_run(self, client, db, make_project):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))

        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(project, script_id=str(script.script_id)),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["target_type"] == "script"
        assert data["script_id"] == str(script.script_id)
        # 09:00 Asia/Shanghai == 01:00 UTC
        assert datetime.fromisoformat(data["next_run_at"]) == datetime(
            2026, 9, 10, 1, 0, tzinfo=timezone.utc
        ) or datetime.fromisoformat(data["next_run_at"]).hour == 1

    async def test_disabled_task_has_no_next_run(self, client, db, make_project):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(project, script_id=str(script.script_id), enabled=False),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["next_run_at"] is None

    async def test_list_scopes_to_project(self, client, db, make_project):
        project = await make_project()
        other = await make_project()
        script = await _make_script(db, str(project.project_id))
        await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(project, script_id=str(script.script_id)),
        )
        resp = await client.get(f"/api/projects/{other.project_id}/schedules")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.parametrize("cron", ["", "not a cron", "0 9 * *", "99 9 * * *", "*/0 * * * *"])
    async def test_rejects_invalid_cron(self, client, db, make_project, cron):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(project, script_id=str(script.script_id), cron_expr=cron),
        )
        assert resp.status_code == 422

    async def test_rejects_unknown_timezone(self, client, db, make_project):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(project, script_id=str(script.script_id), timezone="Mars/Olympus"),
        )
        assert resp.status_code == 422

    async def test_rejects_script_from_another_project(self, client, db, make_project):
        project = await make_project()
        other = await make_project()
        foreign = await _make_script(db, str(other.project_id))
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(project, script_id=str(foreign.script_id)),
        )
        assert resp.status_code == 422

    async def test_rejects_playwright_anchor_as_script_target(self, client, db, make_project):
        project = await make_project()
        anchor = await _make_script(db, str(project.project_id), script_type="playwright")
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(project, script_id=str(anchor.script_id)),
        )
        assert resp.status_code == 422

    async def test_rejects_missing_playwright_spec(self, client, db, make_project):
        project = await make_project()
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(project, target_type="playwright", spec_slug="login-flow"),
        )
        assert resp.status_code == 422

    async def test_accepts_existing_playwright_spec(self, client, db, make_project):
        project = await make_project()
        generated = (
            __import__("pathlib").Path(__import__("os").environ["PROJECTS_ROOT"])
            / project.slug / "tests" / "generated"
        )
        generated.mkdir(parents=True, exist_ok=True)
        (generated / "login-flow.spec.ts").write_text("test.describe('login', () => {})")
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(project, target_type="playwright", spec_slug="login-flow"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["spec_slug"] == "login-flow"

    async def test_agent_target_requires_prompt_and_known_slug(self, client, db, make_project):
        project = await make_project()
        conv = await _make_conversation(db, str(project.project_id))
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(
                project, target_type="agent", agent_slug="tech-lead",
                prompt="   ", conversation_id=str(conv.conversation_id),
            ),
        )
        assert resp.status_code == 422

        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(
                project, target_type="agent", agent_slug="no-such-agent",
                prompt="report", conversation_id=str(conv.conversation_id),
            ),
        )
        assert resp.status_code == 422

    async def test_agent_target_accepts_global_agent(self, client, db, make_project):
        project = await make_project()
        conv = await _make_conversation(db, str(project.project_id))
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules",
            json=_body(
                project, target_type="agent", agent_slug="tech-lead",
                prompt="summarise yesterday's commits",
                conversation_id=str(conv.conversation_id),
            ),
        )
        assert resp.status_code == 200, resp.text

    async def test_rejects_conversation_of_another_user(self, client, db, make_project, as_user):
        project = await make_project()
        foreign = await _make_conversation(db, str(project.project_id), user_id="someone-else")
        async with as_user("test-user"):
            resp = await client.post(
                f"/api/projects/{project.project_id}/schedules",
                json=_body(
                    project, target_type="agent", agent_slug="tech-lead",
                    prompt="hi", conversation_id=str(foreign.conversation_id),
                ),
            )
        assert resp.status_code == 422


class TestUpdate:
    async def test_patch_cron_recomputes_next_run(self, client, db, make_project):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        created = (
            await client.post(
                f"/api/projects/{project.project_id}/schedules",
                json=_body(project, script_id=str(script.script_id)),
            )
        ).json()

        resp = await client.patch(
            f"/api/schedules/{created['schedule_id']}", json={"cron_expr": "*/5 * * * *"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["cron_expr"] == "*/5 * * * *"
        assert resp.json()["next_run_at"] != created["next_run_at"]

    async def test_disabling_clears_next_run(self, client, db, make_project):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        created = (
            await client.post(
                f"/api/projects/{project.project_id}/schedules",
                json=_body(project, script_id=str(script.script_id)),
            )
        ).json()

        resp = await client.patch(
            f"/api/schedules/{created['schedule_id']}", json={"enabled": False}
        )
        assert resp.status_code == 200
        assert resp.json()["next_run_at"] is None
        assert resp.json()["enabled"] is False

    async def test_patch_name_keeps_target(self, client, db, make_project):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        created = (
            await client.post(
                f"/api/projects/{project.project_id}/schedules",
                json=_body(project, script_id=str(script.script_id)),
            )
        ).json()
        resp = await client.patch(
            f"/api/schedules/{created['schedule_id']}", json={"name": "renamed"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed"
        assert resp.json()["script_id"] == str(script.script_id)


class TestDeleteAndRuns:
    async def test_delete_removes_task_and_history(self, client, db, make_project):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        created = (
            await client.post(
                f"/api/projects/{project.project_id}/schedules",
                json=_body(project, script_id=str(script.script_id)),
            )
        ).json()

        run = ScheduledTaskRun(
            schedule_id=created["schedule_id"],
            project_id=str(project.project_id),
            trigger="schedule",
            status="completed",
        )
        db.add(run)
        await db.commit()

        resp = await client.delete(f"/api/schedules/{created['schedule_id']}")
        assert resp.status_code == 200
        assert (await db.execute(
            select(ScheduledTaskRun).where(ScheduledTaskRun.schedule_id == created["schedule_id"])
        )).first() is None
        assert (await db.execute(
            select(ScheduledTask).where(ScheduledTask.schedule_id == created["schedule_id"])
        )).first() is None

    async def test_runs_history_is_returned(self, client, db, make_project):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        created = (
            await client.post(
                f"/api/projects/{project.project_id}/schedules",
                json=_body(project, script_id=str(script.script_id)),
            )
        ).json()
        db.add(ScheduledTaskRun(
            schedule_id=created["schedule_id"],
            project_id=str(project.project_id),
            trigger="schedule",
            status="failed",
            error="boom",
        ))
        await db.commit()

        resp = await client.get(f"/api/schedules/{created['schedule_id']}/runs")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "boom"


class TestRunNow:
    async def test_run_now_launches_and_returns_run_id(
        self, client, db, make_project, monkeypatch
    ):
        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        created = (
            await client.post(
                f"/api/projects/{project.project_id}/schedules",
                json=_body(project, script_id=str(script.script_id)),
            )
        ).json()

        launched: list[tuple[str, str]] = []

        async def fake_spawn(app, schedule_id, *, trigger):
            launched.append((schedule_id, trigger))
            return "run-123"

        monkeypatch.setattr("api.services.scheduled_runs.spawn_run", fake_spawn)
        resp = await client.post(f"/api/schedules/{created['schedule_id']}/run")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"run_id": "run-123", "status": "pending"}
        assert launched == [(created["schedule_id"], "manual")]

    async def test_run_now_conflicts_when_busy(self, client, db, make_project, monkeypatch):
        from api.services.scheduled_runs import ScheduleBusy

        project = await make_project()
        script = await _make_script(db, str(project.project_id))
        created = (
            await client.post(
                f"/api/projects/{project.project_id}/schedules",
                json=_body(project, script_id=str(script.script_id)),
            )
        ).json()

        async def fake_spawn(app, schedule_id, *, trigger):
            raise ScheduleBusy(schedule_id)

        monkeypatch.setattr("api.services.scheduled_runs.spawn_run", fake_spawn)
        resp = await client.post(f"/api/schedules/{created['schedule_id']}/run")
        assert resp.status_code == 409


class TestPreview:
    async def test_preview_returns_three_future_times(self, client, make_project):
        project = await make_project()
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules/preview",
            json={"cron_expr": "0 9 * * *", "timezone": "UTC"},
        )
        assert resp.status_code == 200, resp.text
        runs = [datetime.fromisoformat(x) for x in resp.json()["next_runs"]]
        assert len(runs) == 3
        assert runs == sorted(runs)
        assert all(r.hour == 9 for r in runs)

    async def test_preview_rejects_invalid_expression(self, client, make_project):
        project = await make_project()
        resp = await client.post(
            f"/api/projects/{project.project_id}/schedules/preview",
            json={"cron_expr": "nope", "timezone": "UTC"},
        )
        assert resp.status_code == 422


class TestAuthorization:
    async def test_other_user_cannot_read_private_project_tasks(
        self, client, db, make_project, as_user
    ):
        project = await make_project(created_by="owner", visibility="private")
        script = await _make_script(db, str(project.project_id))
        async with as_user("owner"):
            created = (
                await client.post(
                    f"/api/projects/{project.project_id}/schedules",
                    json=_body(project, script_id=str(script.script_id)),
                )
            ).json()

        async with as_user("intruder"):
            assert (await client.get(f"/api/schedules/{created['schedule_id']}")).status_code == 403
            assert (await client.delete(f"/api/schedules/{created['schedule_id']}")).status_code == 403
            assert (
                await client.get(f"/api/schedules/{created['schedule_id']}/runs")
            ).status_code == 403

    async def test_unknown_schedule_is_404(self, client):
        assert (await client.get("/api/schedules/does-not-exist")).status_code == 404
