"""Access logs must carry the correlation id of their request.

The JSON/Human formatters read request_id_var at FORMAT time, so the
middleware may only reset the var after its own summary line — otherwise
every non-exception access line ships without request_id while the
downstream logs it correlates with still carry it.
"""
from __future__ import annotations

import json
import logging


async def test_success_access_log_carries_request_id(client):
    from api.logging_setup import JSONFormatter

    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(JSONFormatter().format(record))

    logger = logging.getLogger("agents_universe.http")
    handler = _Capture()
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)  # GET /health success logs at DEBUG
    try:
        resp = await client.get("/health")
        assert resp.status_code == 200
        header_id = resp.headers.get("X-Request-Id")
        assert header_id
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    access = [line for line in lines if "HTTP GET /health" in line]
    assert access, lines
    entry = json.loads(access[-1])
    # The request_id on the log line must match the header the client got —
    # that pairing is the whole point of the correlation id.
    assert entry.get("request_id") == header_id
