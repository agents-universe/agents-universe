"""The uncapped tail buffer in the shared subprocess runner.

The stored run log is head-capped so the WS diff view never sees it shrink,
which means a verbose run loses its last lines - exactly where a test runner
prints its verdict. Callers that need that verdict pass a tail buffer.
"""
from __future__ import annotations

import pytest

from agent_core.scripts.runner import TAIL_LINES, drain_stream, new_log_tail


class _Stream:
    """Minimal asyncio.StreamReader stand-in: readline() until exhausted."""

    def __init__(self, lines: list[str]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0).encode() if self._lines else b""


async def _drain(lines: list[str], budget: int, tail=None) -> tuple[list[str], object]:
    log_acc: list[str] = []
    await drain_stream(_Stream(lines), log_acc, [budget], tail)
    return log_acc, tail


@pytest.mark.asyncio
async def test_tail_keeps_the_lines_the_cap_drops():
    first = "x" * 30 + "\n"
    verdict = "5 passed (1.2s)\n"

    log_acc, tail = await _drain([first, verdict], len(first), new_log_tail())

    # The stored log stops at the cap (plus its marker); the tail has the rest.
    assert log_acc == [first, "[executor] output truncated (log cap reached)\n"]
    assert "".join(tail) == first + verdict


@pytest.mark.asyncio
async def test_without_a_tail_buffer_nothing_changes():
    """Existing callers keep the old contract: dropped is dropped."""
    first = "x" * 30 + "\n"
    verdict = "5 passed (1.2s)\n"

    log_acc, _ = await _drain([first, verdict], len(first))

    assert "".join(log_acc) == first + "[executor] output truncated (log cap reached)\n"


@pytest.mark.asyncio
async def test_a_line_overshooting_the_cap_still_marks_the_truncation():
    """Budgets rarely land on zero: a long line overshoots it. The marker keys
    off exactly zero, so without clamping the cut is silent."""
    first = "x" * 30 + "\n"

    log_acc, _ = await _drain([first, "5 passed (1.2s)\n"], budget=10)

    assert log_acc == [first, "[executor] output truncated (log cap reached)\n"]


@pytest.mark.asyncio
async def test_tail_is_bounded():
    """A marathon run must not grow the buffer without limit."""
    lines = [f"line {index}\n" for index in range(TAIL_LINES + 50)]

    log_acc, tail = await _drain(lines, 0, new_log_tail())

    assert len(tail) == TAIL_LINES
    assert tail[-1] == lines[-1]
    assert len("".join(log_acc)) < 64
