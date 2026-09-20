"""The script-run concurrency cap is a host property, so it is configurable.

One global pool is shared by the run button, the agent's script_writer and
scheduled tasks, and a Playwright slot is not cheap (a browser per worker) — on
a host with spare memory it should be possible to raise it without editing code,
and on a small box to lower it. The default must stay put: it is a memory guard.

The semaphore is created lazily, so these tests must not let anything create it
first — hence the reset fixture rather than a value read at import time.
"""
from __future__ import annotations

import pytest

from agent_core.scripts import runner


@pytest.fixture(autouse=True)
def _fresh_semaphore(monkeypatch):
    monkeypatch.setattr(runner, "_script_semaphore", None)
    yield


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 3),          # unset -> the guard stays in place
        ("", 3),            # empty string is unset, not zero
        ("  ", 3),
        ("7", 7),
        ("1", 1),
        ("0", 1),           # never zero: that would deadlock every run
        ("-4", 1),
        ("99", 16),         # clamped to the ceiling rather than accepted
        ("3.5", 3),         # not an integer -> default, not a crash
        ("many", 3),
    ],
)
def test_limit_from_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SCRIPTS_CONCURRENCY_LIMIT", raising=False)
    else:
        monkeypatch.setenv("SCRIPTS_CONCURRENCY_LIMIT", raw)

    assert runner._concurrency_limit() == expected


async def test_semaphore_is_built_from_the_configured_limit(monkeypatch):
    monkeypatch.setenv("SCRIPTS_CONCURRENCY_LIMIT", "5")

    assert runner.script_slot_guard()._value == 5


async def test_semaphore_is_created_once(monkeypatch):
    """The pool is process-wide: a second caller must not get a second pool of
    its own, or the cap would multiply per caller."""
    monkeypatch.setenv("SCRIPTS_CONCURRENCY_LIMIT", "2")

    first = runner.script_slot_guard()
    monkeypatch.setenv("SCRIPTS_CONCURRENCY_LIMIT", "9")

    assert runner.script_slot_guard() is first


def test_an_unparseable_value_is_reported(caplog, monkeypatch):
    monkeypatch.setenv("SCRIPTS_CONCURRENCY_LIMIT", "three")

    with caplog.at_level("WARNING"):
        assert runner._concurrency_limit() == 3

    assert "SCRIPTS_CONCURRENCY_LIMIT" in caplog.text
