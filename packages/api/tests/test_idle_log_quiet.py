"""端到端：挂机时的轮询请求不许产生任何 INFO 日志。

回归的 bug：middleware 那条访问日志降级成了 DEBUG，但 authorize_project
依赖里还留着两条 INFO，而项目级接口全走它——5 秒一次的会话树轮询于是每轮
各打一次，两个打开的标签页就是每分钟 24 行。

所以这里走**真实路由**而不是给 middleware 挂个假 app：只测 middleware 的
话，依赖层的刷屏根本进不了断言视野，测试会绿着放过这个 bug。
"""
from __future__ import annotations

import logging

import pytest


def _info_from_app(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.name.startswith("agents_universe.") and r.levelno >= logging.INFO
    ]


async def test_idle_poll_leaves_no_info_logs(client, make_project, caplog):
    caplog.set_level(logging.INFO)
    project = await make_project()

    resp = await client.get(f"/api/projects/{project.project_id}/conversations")

    assert resp.status_code == 200
    assert _info_from_app(caplog) == []


async def test_the_assertion_can_actually_fail(client, caplog):
    """反例：同一路径的失败请求必须留下 INFO。

    没有这条，上面那个「零 INFO」断言可能只是因为日志全哑了而恒真。
    """
    caplog.set_level(logging.INFO)

    resp = await client.get("/api/projects/no-such-project/conversations")

    assert resp.status_code == 404
    assert any(r.name == "agents_universe.http" and r.levelno == logging.INFO
               for r in caplog.records)
