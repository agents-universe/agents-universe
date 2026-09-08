"""轮询端点的访问日志降级为 DEBUG，其余请求仍打 INFO。

会话树面板每 5 秒拉一次会话列表；只要用户挂着页面，INFO 日志就一行接一行。
"""
from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from api.middleware.logging import StructuredLoggingMiddleware

_LOGGER = "agents_universe.http"


async def _conversations(request):  # noqa: ANN001
    """同一路径的 GET（轮询）与 POST（新建会话）。"""
    if request.method == "POST" or request.query_params.get("fail"):
        return JSONResponse({"detail": "boom"}, status_code=500)
    return JSONResponse({"ok": True})


async def _messages(request):  # noqa: ANN001
    return JSONResponse({"ok": True})


@pytest.fixture
def client() -> AsyncClient:
    app = Starlette(routes=[
        Route("/api/projects/{project_id}/conversations", _conversations, methods=["GET", "POST"]),
        Route("/api/conversations/{conversation_id}/messages", _messages),
    ])
    app.add_middleware(StructuredLoggingMiddleware)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _http_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


async def test_poll_get_logs_at_debug(client, caplog):
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    async with client as c:
        resp = await c.get("/api/projects/p1/conversations?agent_slug=dev")

    assert resp.status_code == 200
    records = _http_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG


async def test_poll_failure_stays_visible(client, caplog):
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    async with client as c:
        resp = await c.get("/api/projects/p1/conversations?fail=1")

    assert resp.status_code == 500
    records = _http_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


async def test_poll_path_write_stays_visible(client, caplog):
    """新建会话走同一路径的 POST —— 不能被降级。"""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    async with client as c:
        resp = await c.post("/api/projects/p1/conversations")

    assert resp.status_code == 500
    records = _http_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


async def test_other_gets_stay_at_info(client, caplog):
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    async with client as c:
        resp = await c.get("/api/conversations/c1/messages")

    assert resp.status_code == 200
    records = _http_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
