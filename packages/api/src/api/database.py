"""SQLAlchemy async engine and session factory."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings

settings = get_settings()

_db_url = settings.effective_db_url
_is_sqlite = _db_url.startswith("sqlite")
_engine_kwargs: dict = {"echo": False}
if not _is_sqlite:
    _engine_kwargs.update({
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,  # raise rather than hang when the pool is exhausted
        "pool_recycle": 3600,  # recycle idle connections after 1h (avoids stale TCP drops)
    })
    if _db_url.startswith("mysql"):
        # MySQL defaults to REPEATABLE READ, which snapshots each transaction
        # at its first read — a session that read before another session
        # committed never sees that write (PostgreSQL/SQL Server default to
        # READ COMMITTED, SQLite reads the latest committed row). Uniform
        # READ COMMITTED keeps cross-session visibility consistent across all
        # four supported dialects and matches the app's short per-request
        # transactions (no multi-statement snapshot consistency is relied on).
        _engine_kwargs["isolation_level"] = "READ COMMITTED"
engine = create_async_engine(_db_url, **_engine_kwargs)


AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
