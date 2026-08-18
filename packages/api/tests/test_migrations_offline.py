"""Offline-compile the full Alembic chain against each non-SQLite dialect.

SQLite is verified live by conftest (the suite runs the real 25-migration
chain every session) and PostgreSQL gets a live CI job. Offline mode never
connects, so this test is how MySQL and the SQL-Server-specific branches get
automated dialect verification: every migration's DDL must compile for the
target dialect without T-SQL leaking into the wrong branch.

Data backfills are skipped in offline mode (they need a live DB) — see the
per-migration ``context.is_offline_mode()`` guards.
"""
import os
from pathlib import Path

import pytest

from alembic import command
from alembic.config import Config
from api.config import get_settings

_ALEMBIC_INI = Path(__file__).parent.parent / "alembic.ini"

# URL → (markers that MUST appear, markers that MUST NOT appear).
# The URLs are async (project convention); env.py maps them to sync drivers.
CASES = {
    "mssql": (
        "mssql+aioodbc://sa:pass@localhost/db?driver=ODBC+Driver+17+for+SQL+Server",
        ["DF_agents_category", "NEWID()", "ALTER COLUMN category", "getutcdate()"],
        [],
    ),
    "postgresql": (
        "postgresql+asyncpg://u:p@localhost/db",
        ["ALTER TABLE agents ALTER COLUMN category SET NOT NULL"],
        ["NEWID()", "DF_agents_category", "GETUTCDATE"],
    ),
    "mysql": (
        "mysql+aiomysql://u:p@localhost/db",
        ["MODIFY category"],
        ["NEWID()", "ADD CONSTRAINT DF_", "INFORMATION_SCHEMA", "GETUTCDATE", "ALTER COLUMN"],
    ),
}


@pytest.mark.parametrize("dialect", sorted(CASES))
def test_chain_compiles_offline(dialect: str, monkeypatch: pytest.MonkeyPatch, capsys):
    """The full migration chain compiles to dialect-valid SQL (no live DB).

    monkeypatch restores TEST_DATABASE_URL after the test; the settings cache
    is cleared both before (so env.py re-reads the URL) and after.
    """
    # env.py imports api.database, which builds an async engine at import time —
    # that resolves the async DBAPI even though offline mode never connects.
    async_driver = {"mysql": "aiomysql", "postgresql": "asyncpg"}.get(dialect)
    if async_driver:
        pytest.importorskip(async_driver)
    url, must_contain, must_not_contain = CASES[dialect]
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    get_settings.cache_clear()
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_INI.parent / "alembic"))
    try:
        command.upgrade(cfg, "head", sql=True)
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert out, f"expected offline SQL output for {dialect}"
    for marker in must_contain:
        assert marker in out, f"{dialect}: expected {marker!r} in compiled SQL"
    for marker in must_not_contain:
        assert marker not in out, f"{dialect}: forbidden {marker!r} leaked into compiled SQL"
