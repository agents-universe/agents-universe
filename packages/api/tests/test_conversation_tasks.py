"""GET /conversations/{id}/tasks 只返回最新一次 plan 的任务。"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from sqlalchemy import select

from api.models.agent import Agent
from api.models.conversation import AgentTask, Conversation
from api.models.task_event import TaskEvent
from api.websocket.handlers import _persist_terminal_task_event, _update_task_status


def _uid() -> str:
    # agent_tasks.task_id / agents.slug 都是全局唯一键，会话级 DB 跨用例
    # 复用，测试数据 id 必须每次不同
    return uuid.uuid4().hex[:8]


async def _make_conversation(client, db, project) -> Conversation:
    slug = f"tasks-tester-{_uid()}"
    db.add(
        Agent(
            slug=slug,
            display_name="Tasks Tester",
            is_system=True,
        )
    )
    await db.commit()
    resp = await client.post(
        f"/api/projects/{project.project_id}/conversations",
        json={"agent_id": slug},
    )
    assert resp.status_code == 200
    conv = await db.get(Conversation, resp.json()["conversation_id"])
    assert conv is not None
    return conv


def _add_task(db, conversation_id: str, task_id: str, title: str, sequence: int, status: str = "pending") -> None:
    db.add(
        AgentTask(
            task_id=task_id,
            conversation_id=conversation_id,
            sequence_num=sequence,
            title=title,
            status=status,
        )
    )


def _add_plan_event(db, conversation_id: str, sequence: int, task_ids: list[str]) -> None:
    db.add(
        TaskEvent(
            conversation_id=conversation_id,
            sequence=sequence,
            event_type="task_plan_created",
            payload=json.dumps({"tasks": [{"id": tid, "title": tid} for tid in task_ids]}),
        )
    )


async def test_returns_only_latest_plan_ordered_by_sequence(client, db, make_project):
    project = await make_project()
    conv = await _make_conversation(client, db, project)
    a, b, c, x, y = (f"t-{_uid()}" for _ in range(5))
    # 第一次 plan：3 个任务（旧行保留在表里，模拟真实累积）
    _add_plan_event(db, conv.conversation_id, 0, [a, b, c])
    _add_task(db, conv.conversation_id, a, "任务A", 0, status="completed")
    _add_task(db, conv.conversation_id, b, "任务B", 1, status="completed")
    _add_task(db, conv.conversation_id, c, "任务C", 2, status="failed")
    # 第二次 plan：2 个任务，故意乱序插入验证排序
    _add_plan_event(db, conv.conversation_id, 1, [x, y])
    _add_task(db, conv.conversation_id, y, "任务Y", 1, status="pending")
    _add_task(db, conv.conversation_id, x, "任务X", 0, status="running")
    await db.commit()

    resp = await client.get(f"/api/conversations/{conv.conversation_id}/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert [t["task_id"] for t in tasks] == [x, y]
    assert tasks[0]["status"] == "running"


async def test_returns_all_tasks_without_plan_event(client, db, make_project):
    # 旧会话没有 task_plan_created 事件 → 回退返回全部行
    project = await make_project()
    conv = await _make_conversation(client, db, project)
    t1, t2 = f"t-{_uid()}", f"t-{_uid()}"
    _add_task(db, conv.conversation_id, t1, "任务1", 0)
    _add_task(db, conv.conversation_id, t2, "任务2", 1)
    await db.commit()

    resp = await client.get(f"/api/conversations/{conv.conversation_id}/tasks")
    assert resp.status_code == 200
    assert {t["task_id"] for t in resp.json()} == {t1, t2}


async def test_returns_all_tasks_when_plan_payload_corrupt(client, db, make_project):
    # 事件 payload 无 tasks 列表（截断/损坏）→ 回退返回全部行
    project = await make_project()
    conv = await _make_conversation(client, db, project)
    db.add(
        TaskEvent(
            conversation_id=conv.conversation_id,
            sequence=0,
            event_type="task_plan_created",
            payload='{"nope": true}',
        )
    )
    t1 = f"t-{_uid()}"
    _add_task(db, conv.conversation_id, t1, "任务1", 0)
    await db.commit()

    resp = await client.get(f"/api/conversations/{conv.conversation_id}/tasks")
    assert resp.status_code == 200
    assert [t["task_id"] for t in resp.json()] == [t1]


async def test_tasks_scoped_to_conversation(client, db, make_project):
    # 过滤不能跨会话：两个会话各自的 plan 互不影响
    project = await make_project()
    conv_a = await _make_conversation(client, db, project)
    conv_b = await _make_conversation(client, db, project)
    a1, b1 = f"t-{_uid()}", f"t-{_uid()}"
    _add_plan_event(db, conv_a.conversation_id, 0, [a1])
    _add_task(db, conv_a.conversation_id, a1, "会话A任务", 0)
    _add_task(db, conv_b.conversation_id, b1, "会话B任务", 0)
    await db.commit()

    resp = await client.get(f"/api/conversations/{conv_b.conversation_id}/tasks")
    assert resp.status_code == 200
    assert [t["task_id"] for t in resp.json()] == [b1]


async def test_update_task_status_skipped_persists_row(client, db, make_project):
    """skipped 状态落库：status / error_message / completed_at 齐全。

    跳过（依赖失败级联/中止）不是失败——落库必须为 skipped，前端才能
    渲染灰色「跳过」而非红色错误。
    """
    project = await make_project()
    conv = await _make_conversation(client, db, project)
    tid = f"t-{_uid()}"
    _add_task(db, conv.conversation_id, tid, "任务X", 0, status="running")
    await db.commit()

    await _update_task_status(db, conv.conversation_id, tid, "skipped", error_message="Aborted")

    row = await db.get(AgentTask, tid)
    assert row is not None
    assert row.status == "skipped"
    assert row.error_message == "Aborted"
    assert row.completed_at is not None, "skipped 任务也应写入 completed_at"


async def test_persist_terminal_task_event_skipped_writes_row_and_log(client, db, make_project):
    """task_skipped 事件经 _persist_terminal_task_event 落库：AgentTask 行
    状态更新 + TaskEvent 日志写入。"""
    project = await make_project()
    conv = await _make_conversation(client, db, project)
    tid = f"t-{_uid()}"
    _add_task(db, conv.conversation_id, tid, "任务Y", 0)
    await db.commit()

    event = SimpleNamespace(type="task_skipped", data={"task_id": tid, "error": "Skipped: dependency failed"})
    await _persist_terminal_task_event(db, conv.conversation_id, event)

    row = await db.get(AgentTask, tid)
    assert row is not None
    assert row.status == "skipped"
    assert row.error_message == "Skipped: dependency failed"

    events = (await db.execute(
        select(TaskEvent).where(
            TaskEvent.conversation_id == conv.conversation_id,
            TaskEvent.event_type == "task_skipped",
        )
    )).scalars().all()
    assert len(events) == 1, f"应写入 1 条 task_skipped 日志，got {len(events)}"
