"""Engine and session management.

Async throughout. SQLite via ``aiosqlite`` today, PostgreSQL via ``asyncpg``
later — the only thing that changes is the URL, because nothing below uses a
dialect-specific API.

SQLite needs three pragmas to behave like a real database under a local API
server plus a CLI plus a background task runner:

* ``journal_mode=WAL`` so a reader does not block the writer;
* ``foreign_keys=ON`` because SQLite ignores foreign keys by default;
* ``busy_timeout`` so a concurrent writer waits instead of raising
  ``database is locked``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from applyuminati.core.errors import StorageError
from applyuminati.core.logging import get_logger
from applyuminati.core.settings import Settings

log = get_logger(__name__)

_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000",
    "PRAGMA synchronous=NORMAL",
)


def _async_url(url: str) -> str:
    """Upgrade a sync URL to its async driver equivalent."""
    if url.startswith("sqlite+pysqlite://"):
        return url.replace("sqlite+pysqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    return url


def sync_url(url: str) -> str:
    """Downgrade an async URL for Alembic, which runs migrations synchronously."""
    if "+aiosqlite" in url:
        return url.replace("+aiosqlite", "+pysqlite", 1)
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "+psycopg", 1)
    return url


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for pragma in _SQLITE_PRAGMAS:
                cursor.execute(pragma)
        finally:
            cursor.close()


def create_engine(settings: Settings, *, echo: bool = False) -> AsyncEngine:
    """Build the async engine for ``settings``.

    In-memory SQLite gets a :class:`StaticPool` so every session in a test
    shares one connection; otherwise each session would see an empty database.
    """
    url = _async_url(settings.resolved_database_url)
    kwargs: dict[str, Any] = {"echo": echo, "future": True}

    if url.startswith("sqlite"):
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            db_file = Path(settings.db_path)
            db_file.parent.mkdir(parents=True, exist_ok=True)
    else:
        kwargs["pool_pre_ping"] = True

    engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):
        _install_sqlite_pragmas(engine)
    return engine


class Database:
    """Owns the engine and hands out sessions.

    One instance per process. The FastAPI app and the CLI both build one from
    the same :class:`Settings`, so they share a single connection policy.
    """

    def __init__(self, settings: Settings, *, echo: bool = False) -> None:
        self.settings = settings
        self.engine = create_engine(settings, echo=echo)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that commits on success and rolls back on any exception."""
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        """A read-only session: never commits, always rolls back."""
        session = self.session_factory()
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()

    async def check(self) -> bool:
        """Connectivity probe used by ``/api/v1/health`` and ``doctor``."""
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            log.warning("database.check_failed", error=str(exc))
            return False
        return True

    async def schema_version(self) -> str | None:
        """Current Alembic revision, or ``None`` when unmigrated."""
        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                row = result.first()
        except Exception:
            return None
        return str(row[0]) if row else None

    async def create_all(self) -> None:
        """Create tables directly from metadata.

        Tests only. Production schema changes go through Alembic so that an
        existing local database is upgraded rather than silently diverging.
        """
        from applyuminati.db import models  # noqa: F401  - registers mappers
        from applyuminati.db.base import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


_database: Database | None = None


def get_database(settings: Settings | None = None) -> Database:
    """Return the process database, creating it on first use."""
    global _database  # noqa: PLW0603 - deliberate process-wide singleton
    if _database is None:
        if settings is None:
            msg = "database not initialised; call get_database(settings) first"
            raise StorageError(msg, code="storage.not_initialised")
        _database = Database(settings)
    return _database


def set_database(database: Database | None) -> None:
    """Replace (or clear) the process database. Used by tests and app startup."""
    global _database  # noqa: PLW0603
    _database = database


__all__ = [
    "Database",
    "create_engine",
    "get_database",
    "set_database",
    "sync_url",
]
