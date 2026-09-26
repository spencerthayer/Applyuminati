"""Shared test fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest

from applyuminati.core.settings import Settings, set_settings
from applyuminati.db.session import Database, set_database
from applyuminati.services.container import get_container, set_container

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        environment="ci",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'data' / 'test.db'}",
    )


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    settings.ensure_directories()
    db = Database(settings)
    await db.create_all()
    set_database(db)
    yield db
    await db.dispose()
    set_database(None)


@pytest.fixture
async def container(settings: Settings, database: Database):
    """A ServiceContainer bound to this test's database.

    Process-wide singletons are reset either side, so one test's container
    cannot serve another test's data.
    """
    from applyuminati.services.container import ServiceContainer, set_container

    set_container(ServiceContainer(settings, database=database))
    try:
        yield get_container()
    finally:
        set_container(None)


@pytest.fixture(autouse=True)
def _dispose_process_resources():
    """Close process-wide resources a test may have built.

    The CLI commands, the ServiceContainer, and the database are process
    singletons. A test that runs a command builds a real container on a real
    data directory, and an undisposed engine leaves a daemon aiosqlite thread
    that raises "Event loop is closed" during whichever later test happens to be
    running. Closing them here is hygiene, not suppression: the warning stays
    fatal, so a genuine leak in test code still fails.
    """
    yield
    from applyuminati.services.container import get_container

    async def _close() -> None:
        container = get_container()
        if container is not None:
            with suppress(Exception):
                await container.aclose()
        database = _peek_database()
        if database is not None:
            with suppress(Exception):
                await database.dispose()

    with suppress(Exception):
        asyncio.run(_close())
    set_container(None)
    set_database(None)
    set_settings(None)


def _peek_database():
    from applyuminati.db import session as session_module

    return session_module._database


@pytest.fixture
def sample_resume() -> dict:
    return {
        "basics": {
            "name": "Jane Engineer",
            "label": "Senior Software Engineer",
            "email": "jane@example.com",
            "summary": "Engineer who builds things.",
        },
        "work": [
            {
                "name": "Acme Corp",
                "position": "Senior Engineer",
                "startDate": "2020-01",
                "endDate": "2024-06",
                "highlights": [
                    "Led migration of billing to Kafka, reducing latency by 40%",
                    "Built platform serving 2M requests/day",
                ],
            },
        ],
        "education": [{"institution": "MIT", "area": "Computer Science", "studyType": "B.S."}],
        "skills": [
            {"name": "Programming", "keywords": ["Python", "TypeScript", "SQL", "Kafka"]},
        ],
    }
