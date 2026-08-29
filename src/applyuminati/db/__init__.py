"""Persistence: portable SQLAlchemy 2 tables and async sessions.

The dialect story in one line: SQLite via ``aiosqlite`` today, PostgreSQL via
``asyncpg`` later — nothing in this package uses a dialect-specific API, and
JSON columns become ``JSONB`` automatically on Postgres through the variant
declared in :mod:`applyuminati.db.base`.
"""

from applyuminati.db.base import ULID, Base, JSONText, UTCDateTime
from applyuminati.db.models import (
    ApplicationArtifactRow,
    ApplicationEventRow,
    ApplicationRow,
    ClaimRow,
    CompanyResearchRow,
    FitScoreRow,
    JobRow,
    JobSourceRow,
    LearningSignalRow,
    LLMCallRow,
    MemoryRow,
    OutcomeRow,
    ProfileRow,
    RunRow,
    SourceStateRow,
    TaskRow,
)
from applyuminati.db.session import Database, create_engine, get_database, set_database, sync_url

__all__ = [
    "ULID",
    "ApplicationArtifactRow",
    "ApplicationEventRow",
    "ApplicationRow",
    "Base",
    "ClaimRow",
    "CompanyResearchRow",
    "Database",
    "FitScoreRow",
    "JSONText",
    "JobRow",
    "JobSourceRow",
    "LLMCallRow",
    "LearningSignalRow",
    "MemoryRow",
    "OutcomeRow",
    "ProfileRow",
    "RunRow",
    "SourceStateRow",
    "TaskRow",
    "UTCDateTime",
    "create_engine",
    "get_database",
    "set_database",
    "sync_url",
]
