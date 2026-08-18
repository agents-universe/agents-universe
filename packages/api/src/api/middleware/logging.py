"""Structured request logging middleware with correlation ID propagation."""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..logging_setup import request_id_var

_log = logging.getLogger("agents_universe.http")


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """Log every HTTP request/response and propagate request_id via ContextVar."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())[:8]
        token = request_id_var.set(request_id)
        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            _log.error(
                "HTTP %s %s -> unhandled exception (%.1fms)",
                request.method, request.url.path, duration_ms,
                exc_info=True,
            )
            raise
        finally:
            request_id_var.reset(token)

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        _log.info(
            "HTTP %s %s -> %d (%.1fms)",
            request.method, request.url.path, response.status_code, duration_ms,
        )

        response.headers["X-Request-Id"] = request_id
        return response
