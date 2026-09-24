"""Structured request logging middleware with correlation ID propagation."""
from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..logging_setup import request_id_var

_log = logging.getLogger("agents_universe.http")

# A single always-quiet-path allowlist rots: the web UI polls on a timer, and
# every new poller added later would need its path appended here or the log
# would start flooding again. So the default is inverted — routine successful
# reads are quiet, and only writes, failures and a few audited paths stay at
# INFO. Failures are checked first, so a broken poller is still visible.
_ALWAYS_INFO_PATHS = (
    re.compile(r"^/auth/"),  # login / OAuth callback / logout
)


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """Log every HTTP request/response and propagate request_id via ContextVar."""

    @staticmethod
    def _access_level(request: Request, response: Response) -> int:
        if response.status_code >= 400:
            return logging.INFO
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            return logging.INFO
        if any(p.match(request.url.path) for p in _ALWAYS_INFO_PATHS):
            return logging.INFO
        return logging.DEBUG

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())[:8]
        token = request_id_var.set(request_id)
        start = time.perf_counter()

        # The var may only be reset AFTER the summary line below: both
        # formatters read it at format time, and resetting first shipped
        # every non-exception access line without its request_id (the error
        # path logs inside the except, before the reset, so it always had it).
        try:
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

            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            _log.log(
                self._access_level(request, response),
                "HTTP %s %s -> %d (%.1fms)",
                request.method, request.url.path, response.status_code, duration_ms,
            )

            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            request_id_var.reset(token)
