"""WebSocket 连接生命周期日志降为 DEBUG，握手失败保持 INFO。

uvicorn 的 WS 协议把 logger 显式传成 uvicorn.error 给 websockets 库，所以
每次连接建立/断开都会在那个 logger 上打几行 INFO——而 uvicorn.access 的静音
管不到它。前端断线后是永不放弃的重连阶梯，代理一抖动就变成持续刷屏。
"""
from __future__ import annotations

import logging

from api.logging_setup import WebSocketLifecycleFilter


def _record(msg: str, level: int = logging.INFO,
            name: str = "uvicorn.error") -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


def test_connection_open_is_demoted():
    rec = _record("connection open")
    assert WebSocketLifecycleFilter().filter(rec) is True
    assert rec.levelno == logging.DEBUG
    assert rec.levelname == "DEBUG"


def test_connection_closed_is_demoted():
    rec = _record("connection closed")
    WebSocketLifecycleFilter().filter(rec)
    assert rec.levelno == logging.DEBUG


def test_accepted_handshake_is_demoted():
    """每次连接一条，前端重连时最先刷屏的就是它。"""
    rec = _record('127.0.0.1:53210 - "WebSocket /ws/conversations/c1" [accepted]')
    WebSocketLifecycleFilter().filter(rec)
    assert rec.levelno == logging.DEBUG


def test_failed_handshake_stays_visible():
    """403 意味着鉴权坏了，必须留在 INFO。"""
    rec = _record('127.0.0.1:53210 - "WebSocket /ws/conversations/c1" 403')
    WebSocketLifecycleFilter().filter(rec)
    assert rec.levelno == logging.INFO


def test_unrelated_uvicorn_messages_are_untouched():
    """同一个 logger 还扛着启动信息和协议 traceback，不能一起压掉。"""
    rec = _record("Application startup complete.")
    WebSocketLifecycleFilter().filter(rec)
    assert rec.levelno == logging.INFO


def test_higher_levels_are_never_demoted():
    rec = _record("connection closed", level=logging.ERROR)
    WebSocketLifecycleFilter().filter(rec)
    assert rec.levelno == logging.ERROR


def test_filter_is_attached_to_the_live_loggers():
    """conftest 导入 api.main 时已跑过 setup_logging，过滤器应当挂上了。

    uvicorn.error 是实际路径；websockets.server 是库默认 logger，防别的
    协议实现绕过 uvicorn 的 logger 注入。
    """
    for name in ("uvicorn.error", "websockets.server"):
        assert any(isinstance(f, WebSocketLifecycleFilter)
                   for f in logging.getLogger(name).filters), name
