"""Startup-time auto-migration behavior."""
from __future__ import annotations

import asyncio

import alembic.command


async def test_migration_retry_skips_sleep_after_the_last_attempt(monkeypatch):
    """Three failures cost two retries — sleeping after the final attempt
    would stall startup before the closing warning."""
    from api import main as api_main

    attempts: list[str] = []
    sleeps: list[int] = []

    def _boom(cfg, revision):
        attempts.append(revision)
        raise RuntimeError("db not ready")

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(alembic.command, "upgrade", _boom)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    await api_main._run_migrations()

    assert len(attempts) == 3
    assert sleeps == [2, 4]
