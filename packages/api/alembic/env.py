"""Alembic migration environment."""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from api.config import get_settings
from api.database import Base
import api.models  # ensure all models are imported

config = context.config
settings = get_settings()

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _sync_migration_url() -> str:
    """Convert the application's async URL into Alembic's synchronous URL."""
    url = make_url(settings.effective_db_url)
    if url.drivername == "mssql+aioodbc":
        url = url.set(drivername="mssql+pyodbc")
    elif url.drivername == "sqlite+aiosqlite":
        url = url.set(drivername="sqlite")
    elif url.drivername == "postgresql+asyncpg":
        url = url.set(drivername="postgresql+psycopg2")
    elif url.drivername == "mysql+aiomysql":
        url = url.set(drivername="mysql+pymysql")
    return url.render_as_string(hide_password=False)


def _make_engine():
    return create_engine(_sync_migration_url(), poolclass=pool.NullPool)


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = _make_engine()
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
