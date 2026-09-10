"""JSON encoding for payloads that come from outside our own code.

Tool results, task events and LLM-produced structures are plain dicts in
principle, but "plain" is an assumption, not a guarantee: a tool that reports
a timestamp hands back a `datetime`, and the stdlib encoder raises TypeError on
one. Every such raise on a persistence path costs the WHOLE row — production
lost an assistant message with 79 tool calls to a single datetime inside a tool
result. Encoding these payloads therefore always goes through `json_default`.

The fallback is deliberately narrow: dates become ISO strings (recoverable,
lossless) and anything else still raises, because silently stringifying an
unexpected type would hide the bug that produced it.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any


def json_default(obj: object) -> Any:
    """`json.dumps(default=...)` hook: dates to ISO strings, everything else is
    a real error."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def dumps(payload: Any, **kwargs: Any) -> str:
    """`json.dumps` with the date-safe default already applied."""
    kwargs.setdefault("default", json_default)
    return json.dumps(payload, **kwargs)
