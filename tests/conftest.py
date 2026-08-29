"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from applyuminati.core.settings import Settings
from applyuminati.db.session import Database, set_database

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
