"""A config file must configure sources.

``SourceService.sync_from_settings`` is documented as the startup path for
file-based source configuration, which is how a Docker or pip install is
configured. It had no callers, so a config.toml naming a source did
nothing and discovery silently found nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from applyuminati.core.settings import Settings
from applyuminati.db.session import Database, set_database
from applyuminati.services.container import ServiceContainer
from applyuminati.services.source_service import SourceService


@pytest.fixture
def migrated_settings(tmp_path: Path) -> Settings:
    """A Settings whose data dir has a schema.

    A container does not migrate; `applyuminati init` does. Creating the schema
    here keeps this test about the sync, not about migration.
    """
    (tmp_path / "config.toml").write_text(CONFIG)
    settings = Settings(data_dir=tmp_path)
    settings.ensure_directories()
    set_database(None)
    return settings


async def _container(settings: Settings) -> ServiceContainer:
    database = Database(settings)
    await database.create_all()
    set_database(database)
    return ServiceContainer(settings, database=database)


CONFIG = """
[discovery.sources.greenhouse]
enabled = true

[discovery.sources.greenhouse.options]
boards = ["example"]
"""


async def test_sync_applies_the_file_to_the_database(migrated_settings: Settings) -> None:
    settings = migrated_settings
    container = await _container(settings)
    async with container.read_repositories() as repos:
        before = {v.slug: v for v in await SourceService(repos, settings).list(probe_health=False)}
    assert before["greenhouse"].enabled is False

    assert await container.sync_settings_to_db() == 1

    async with container.read_repositories() as repos:
        after = {v.slug: v for v in await SourceService(repos, settings).list(probe_health=False)}
    assert after["greenhouse"].enabled is True
    assert after["greenhouse"].options == {"boards": ["example"]}


async def test_sync_is_idempotent(migrated_settings: Settings) -> None:
    container = await _container(migrated_settings)
    assert await container.sync_settings_to_db() == 1
    assert await container.sync_settings_to_db() == 0


async def test_a_source_the_file_does_not_mention_keeps_its_state(
    migrated_settings: Settings,
) -> None:
    """The file is authoritative only for the sources it names."""
    settings = migrated_settings
    container = await _container(settings)
    # Separate units of work: a write held open here would block the sync's
    # own session, which is the same contention t01 fixed in the worker.
    async with container.repositories() as repos:
        await SourceService(repos, settings).set_enabled("local_feed", True)
    await container.sync_settings_to_db()
    async with container.read_repositories() as repos:
        views = {v.slug: v for v in await SourceService(repos, settings).list(probe_health=False)}
    assert views["local_feed"].enabled is True
