"""Centralized logging configuration for the Agents Universe API."""
from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# ── Context Variables ─────────────────────────────────────────────────────────

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
correlation_ctx_var: ContextVar[dict[str, str]] = ContextVar(
    "correlation_ctx", default={}
)


# ── JSON Formatter ────────────────────────────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line with correlation context."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        req_id = request_id_var.get("")
        if req_id:
            entry["request_id"] = req_id
        ctx = correlation_ctx_var.get({})
        if ctx:
            entry.update(ctx)
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


# ── Human Formatter ───────────────────────────────────────────────────────────


class HumanFormatter(logging.Formatter):
    """Colored, human-readable format for local development."""

    FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    def __init__(self) -> None:
        super().__init__(self.FMT, datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        req_id = request_id_var.get("")
        ctx = correlation_ctx_var.get({})
        extra_parts: list[str] = []
        if req_id:
            extra_parts.append(f"req={req_id}")
        if ctx.get("conversation_id"):
            extra_parts.append(f"conv={ctx['conversation_id'][:8]}")
        if ctx.get("project_id"):
            extra_parts.append(f"proj={ctx['project_id'][:8]}")
        if ctx.get("user_id"):
            extra_parts.append(f"user={ctx['user_id'][:8]}")
        if extra_parts:
            record = logging.makeLogRecord(record.__dict__)
            record.msg = f"[{' '.join(extra_parts)}] {record.msg}"
        return super().format(record)


# ── Context-injecting Filter ─────────────────────────────────────────────────


class CorrelationFilter(logging.Filter):
    """Adds contextvar fields to every LogRecord for programmatic access."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("")  # type: ignore[attr-defined]
        ctx = correlation_ctx_var.get({})
        record.conversation_id = ctx.get("conversation_id", "")  # type: ignore[attr-defined]
        record.project_id = ctx.get("project_id", "")  # type: ignore[attr-defined]
        record.user_id = ctx.get("user_id", "")  # type: ignore[attr-defined]
        record.task_id = ctx.get("task_id", "")  # type: ignore[attr-defined]
        return True


# ── Setup Function ────────────────────────────────────────────────────────────


def setup_logging() -> None:
    """Configure root logger. Call once from create_app()."""
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_format = os.environ.get("LOG_FORMAT", "json")

    root = logging.getLogger()
    root.setLevel(log_level)

    # Remove existing handlers to avoid duplicates on reload
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)

    if log_format == "human":
        handler.setFormatter(HumanFormatter())
    else:
        handler.setFormatter(JSONFormatter())

    handler.addFilter(CorrelationFilter())
    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
