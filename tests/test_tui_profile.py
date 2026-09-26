"""The profile screen: import a career profile and show what was derived.

The screen is the first thing a new user sees, so an empty database must
render an actionable next step rather than a blank panel, and every failure
must land in a message the user can read.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual", reason="the tui extra is not installed")

from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Static, TextArea

from applyuminati.core.errors import NotFoundError
from applyuminati.services.container import ServiceContainer
from applyuminati.services.profile_service import ProfileService

Predicate = Callable[[], bool]


class _ProfileApp(App[None]):
    """The smallest host that satisfies the screen's only app dependency."""

    def __init__(self, container: ServiceContainer) -> None:
        super().__init__()
        self._container = container

    @property
    def container(self) -> ServiceContainer:
        return self._container

    def compose(self) -> ComposeResult:
        from applyuminati.tui.screens.profile import ProfileScreen

        yield ProfileScreen()


async def _wait_for(pilot: Any, done: Predicate) -> None:
    """Wait until ``done`` holds, yielding to the app's loop meanwhile."""
    for _ in range(150):
        await pilot.pause()
        if done():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the profile screen never settled")


def _text(app: App[None], selector: str) -> str:
    try:
        widget = app.screen.query_one(selector, Static)
    except NoMatches:  # the screen is still being composed
        return ""
    return str(widget.content)


def _summary(app: App[None]) -> str:
    return _text(app, "#profile-summary")


def _error(app: App[None]) -> str:
    return _text(app, "#profile-error")


def _status(app: App[None]) -> str:
    return _text(app, "#profile-status")


def _loaded(app: App[None]) -> bool:
    """The first paint is a loading line; anything after it is a decision."""
    return not _summary(app).startswith("Loading")


def _shown(app: App[None]) -> bool:
    """The screen is the active one and has finished its first read."""
    return bool(app.screen.query("#profile-summary")) and _loaded(app)


async def _paste(app: App[None], pilot: Any, document: str, until: Predicate) -> None:
    def trigger() -> None:
        app.screen.query_one("#profile-document", TextArea).text = document
        app.screen.query_one("#import-document", Button).press()

    trigger()
    await _wait_for(pilot, until)


async def _import_path(app: App[None], pilot: Any, path: Path | str, until: Predicate) -> None:
    field = app.screen.query_one("#profile-path", Input)
    field.value = str(path)
    app.screen.set_focus(field)
    await pilot.press("enter")
    await _wait_for(pilot, until)


async def _stored(container: ServiceContainer) -> dict[str, Any] | None:
    async with container.read_repositories() as repos:
        try:
            view = await ProfileService(repos).view()
        except NotFoundError:
            return None
    return {
        "profile_id": view.profile_id,
        "created_at": view.created_at,
        "name": view.name,
        "counts": view.counts,
    }


async def _active_profile_ids(container: ServiceContainer) -> list[str]:
    from sqlalchemy import select

    from applyuminati.db.models import ProfileRow

    async with container.database.read_session() as session:
        rows = await session.scalars(
            select(ProfileRow.id).where(ProfileRow.is_active).order_by(ProfileRow.id)
        )
        return list(rows.all())


async def test_the_empty_state_names_the_next_action(container: ServiceContainer) -> None:
    """A new user must be told what to do, not shown a blank panel."""
    app = _ProfileApp(container)
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: _shown(app))

        summary = _summary(app)
        assert "No career profile" in summary
        # The next action, in the two forms the screen actually offers.
        assert "paste" in summary.lower()
        assert "path" in summary.lower()
        assert "JSON Resume" in summary
        assert _error(app) == ""


async def test_importing_a_pasted_document_stores_the_profile_and_renders_counts(
    container: ServiceContainer, sample_resume: dict
) -> None:
    app = _ProfileApp(container)
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: _shown(app))
        assert await _stored(container) is None

        await _paste(
            app,
            pilot,
            json.dumps(sample_resume),
            until=lambda: "Jane Engineer" in _summary(app),
        )

        assert _error(app) == ""
        stored = await _stored(container)
        assert stored is not None
        assert stored["name"] == "Jane Engineer"
        assert stored["counts"]["claims"] == 4
        assert stored["counts"]["metrics"] == 1

        summary = _summary(app)
        assert "Senior Software Engineer" in summary
        assert f"claims: {stored['counts']['claims']}" in summary
        assert f"metrics: {stored['counts']['metrics']}" in summary
        assert "No career profile" not in summary

        levels = _text(app, "#profile-claim-levels")
        assert "verified" in levels
        assert f"verified: {stored['counts']['claims']}" in levels


async def test_importing_from_a_path_renders_the_same_summary(
    container: ServiceContainer, sample_resume: dict, tmp_path: Path
) -> None:
    resume_file = tmp_path / "resume.json"
    resume_file.write_text(json.dumps(sample_resume), encoding="utf-8")

    app = _ProfileApp(container)
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: _shown(app))

        await _import_path(app, pilot, resume_file, until=lambda: "Jane Engineer" in _summary(app))

        assert _error(app) == ""
        stored = await _stored(container)
        assert stored is not None
        assert stored["name"] == "Jane Engineer"


async def test_a_document_that_is_not_json_is_reported_not_crashed(
    container: ServiceContainer,
) -> None:
    app = _ProfileApp(container)
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: _shown(app))

        await _paste(
            app,
            pilot,
            "I pasted my resume but it is not JSON",
            until=lambda: _error(app) != "",
        )

        assert "JSON" in _error(app)
        # Still alive, still showing the empty state, nothing stored — and the
        # status line must not be left mid-flight.
        assert app.is_running
        assert "Importing" not in _status(app)
        assert "No career profile" in _summary(app)
        assert await _stored(container) is None


async def test_a_document_that_fails_validation_is_reported(
    container: ServiceContainer,
) -> None:
    """A JSON object of the wrong shape reaches the importer, which rejects it."""
    app = _ProfileApp(container)
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: _shown(app))

        await _paste(
            app,
            pilot,
            json.dumps({"basics": {"name": 5}}),
            until=lambda: _error(app) != "",
        )

        assert "name" in _error(app)
        assert await _stored(container) is None


async def test_a_missing_file_path_is_reported(container: ServiceContainer) -> None:
    app = _ProfileApp(container)
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: _shown(app))

        await _import_path(
            app, pilot, Path("/nonexistent/resume.json"), until=lambda: _error(app) != ""
        )

        assert "/nonexistent/resume.json" in _error(app)


async def test_importing_the_path_without_one_asks_for_the_path(
    container: ServiceContainer,
) -> None:
    app = _ProfileApp(container)
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: _shown(app))

        await _import_path(app, pilot, "   ", until=lambda: _error(app) != "")

        assert "path" in _error(app).lower()


async def test_a_second_import_replaces_the_active_profile_in_place(
    container: ServiceContainer, sample_resume: dict, tmp_path: Path
) -> None:
    """``replace=True`` reuses the profile id, so no duplicate appears."""
    other = {
        "basics": {"name": "Bob Builder", "label": "Platform Engineer"},
        "work": [
            {
                "name": "Initech",
                "position": "Engineer",
                "startDate": "2019-01",
                "highlights": ["Cut deploy time by 30%"],
            }
        ],
    }
    other_file = tmp_path / "other.json"
    other_file.write_text(json.dumps(other), encoding="utf-8")

    app = _ProfileApp(container)
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: _shown(app))

        await _paste(
            app, pilot, json.dumps(sample_resume), until=lambda: "Jane Engineer" in _summary(app)
        )
        first = await _stored(container)
        assert first is not None

        await _import_path(app, pilot, other_file, until=lambda: "Bob Builder" in _summary(app))
        second = await _stored(container)

        assert second is not None
        assert _error(app) == ""
        assert await _active_profile_ids(container) == [first["profile_id"]]
        assert second["profile_id"] == first["profile_id"]
        assert second["created_at"] == first["created_at"]
        assert second["name"] == "Bob Builder"


async def test_import_warnings_are_surfaced(container: ServiceContainer) -> None:
    """The importer never fabricates: unusable fields come back as warnings."""
    app = _ProfileApp(container)
    async with app.run_test() as pilot:
        await _wait_for(pilot, lambda: _shown(app))

        await _paste(
            app,
            pilot,
            json.dumps({"work": [{"name": "Acme Corp"}]}),
            until=lambda: "warning" in _status(app).lower(),
        )

        assert _error(app) == ""
        assert await _stored(container) is not None


async def test_the_screen_renders_inside_the_real_app(
    container: ServiceContainer, sample_resume: dict, tmp_path: Path
) -> None:
    """Proof the screen is usable on the app the CLI actually launches."""
    from applyuminati.tui.app import TuiApp
    from applyuminati.tui.screens.profile import ProfileScreen

    resume_file = tmp_path / "resume.json"
    resume_file.write_text(json.dumps(sample_resume), encoding="utf-8")

    app = TuiApp(container)
    async with app.run_test() as pilot:
        app.push_screen(ProfileScreen())
        await _wait_for(pilot, lambda: _shown(app))

        await _import_path(app, pilot, resume_file, until=lambda: "Jane Engineer" in _summary(app))

        assert "Jane Engineer" in _summary(app)
