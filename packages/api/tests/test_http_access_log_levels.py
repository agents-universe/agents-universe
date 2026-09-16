"""访问日志的级别策略：成功的常规读取静默，写操作和失败可见。

用户只要挂着页面，会话树每 5 秒的轮询就会持续产生访问日志。按路径白名单
逐条降级会随新端点腐化——每加一个轮询接口都要记得补一次——所以策略反过来：
默认 DEBUG，只有 4xx/5xx、写操作和少数需要留痕的路径（登录/回调/登出）保持 INFO。
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


async def _handler(request):  # noqa: ANN001
    """同一路径的读/写共用；?fail=1 模拟失败响应。"""
    if request.query_params.get("fail"):
        return JSONResponse({"detail": "boom"}, status_code=500)
    return JSONResponse({"ok": True})


@pytest.fixture
def client() -> AsyncClient:
    app = Starlette(routes=[
        Route("/api/projects/{project_id}/conversations", _handler, methods=["GET", "POST"]),
        Route("/api/media/{project_id}/{conversation_id}/{filename}", _handler),
        Route("/health", _handler),
        Route("/auth/login", _handler),
    ])
    app.add_middleware(StructuredLoggingMiddleware)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _http_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


async def _levels(client: AsyncClient, caplog, method: str, path: str) -> list[int]:
    """发一个请求，返回 middleware 记下的级别列表（中间件一行，故长度为 1）。"""
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    async with client as c:
        await c.request(method, path)
    return [r.levelno for r in _http_records(caplog)]


# ── 静默的一侧 ──────────────────────────────────────────────────────

async def test_idle_poll_read_is_quiet(client, caplog):
    """会话树 5 秒轮询的路径——挂机刷屏的主角。"""
    assert await _levels(client, caplog, "GET", "/api/projects/p1/conversations") == [logging.DEBUG]


async def test_media_read_is_quiet(client, caplog):
    """聊天里每张图片一次 GET，之前每次都打 INFO。"""
    levels = await _levels(client, caplog, "GET", "/api/media/p1/c1/shot.png")
    assert levels == [logging.DEBUG]


async def test_health_read_is_quiet(client, caplog):
    """Docker 健康检查每 30 秒一次，没人用的时候也在刷。"""
    assert await _levels(client, caplog, "GET", "/health") == [logging.DEBUG]


# ── 可见的一侧 ──────────────────────────────────────────────────────

async def test_read_failure_stays_visible(client, caplog):
    """坏掉的轮询必须看得见，否则静音就成了掩盖。"""
    levels = await _levels(client, caplog, "GET", "/api/projects/p1/conversations?fail=1")
    assert levels == [logging.INFO]


async def test_write_stays_visible(client, caplog):
    """同路径的 POST（新建会话）不能被当成轮询一起静音。"""
    levels = await _levels(client, caplog, "POST", "/api/projects/p1/conversations")
    assert levels == [logging.INFO]


async def test_auth_path_stays_visible(client, caplog):
    """登录/回调/登出是 GET，但属于要留痕的一次性事件。"""
    assert await _levels(client, caplog, "GET", "/auth/login") == [logging.INFO]
