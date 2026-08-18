"""DATABASE_URL precedence and default-URL regression tests.

Settings instances are constructed directly with explicit kwargs (init args
outrank env vars in pydantic-settings) so the global get_settings() cache and
the test-session TEST_DATABASE_URL are never disturbed.
"""
from api.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(
        test_database_url="",
        database_url="",
        mssql_connection_string="",
        db_host="127.0.0.1",
        db_port=1433,
        db_name="agentsuniverse",
        db_user="sa",
        db_password="YourPassword",
        db_driver="ODBC Driver 17 for SQL Server",
        db_trust_cert=True,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_default_url_is_byte_identical_mssql_build():
    """No overrides → the exact mssql+aioodbc URL built from DB_* fields (unchanged)."""
    s = _settings()
    assert s.effective_db_url == (
        "mssql+aioodbc://sa:YourPassword@127.0.0.1:1433/agentsuniverse"
        "?driver=ODBC+Driver+17+for+SQL+Server&TrustServerCertificate=yes"
    )


def test_database_url_beats_mssql_connection_string_and_db_fields():
    s = _settings(
        database_url="postgresql+asyncpg://u:p@dbhost:5432/appdb",
        mssql_connection_string="mssql+aioodbc://legacy:legacy@oldhost/db?driver=X",
        db_host="127.0.0.1",
        db_port=1433,
    )
    assert s.effective_db_url == "postgresql+asyncpg://u:p@dbhost:5432/appdb"


def test_mssql_connection_string_is_legacy_fallback():
    s = _settings(mssql_connection_string="mysql+aiomysql://u:p@h/db")
    assert s.effective_db_url == "mysql+aiomysql://u:p@h/db"


def test_test_database_url_still_wins_everything():
    s = _settings(
        test_database_url="sqlite+aiosqlite:///tmp/test.db",
        database_url="postgresql+asyncpg://u:p@h/db",
        mssql_connection_string="mssql+aioodbc://legacy",
    )
    assert s.effective_db_url == "sqlite+aiosqlite:///tmp/test.db"


def test_trust_cert_flag_off_drops_query_param():
    s = _settings(db_trust_cert=False)
    assert s.effective_db_url.endswith("?driver=ODBC+Driver+17+for+SQL+Server")
    assert "TrustServerCertificate" not in s.effective_db_url
