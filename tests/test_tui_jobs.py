"""The Jobs screen lists real jobs and filters them honestly.

These assertions are about *content*, not widget presence: a table that mounts
but renders nothing is the failure mode that matters here, so every test reads
cells back out of the rendered rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from applyuminati.core.models.job import SourceTier
from applyuminati.services.container import ServiceContainer
from applyuminati.sources.normalize import build_job
from applyuminati.tui.app import TuiApp
from applyuminati.tui.screens.jobs import JobsScreen

if TYPE_CHECKING:
    from textual.pilot import Pilot
    from textual.widgets import DataTable


def _job(*, source_job_id: str, company: str, title: str) -> Any:
    return build_job(
        source="linkedin",
        tier=SourceTier.AGGREGATOR,
        source_job_id=source_job_id,
        url=f"https://www.linkedin.com/jobs/view/{source_job_id}",
        title=title,
        company=company,
    )


class _RecordingApp(TuiApp):
    """Captures the job ids the screen reports, standing in for the detail screen."""

    def __init__(self, container: ServiceContainer) -> None:
        super().__init__(container)
        self.selected: list[str] = []

    def on_job_selected(self, event: Any) -> None:
        self.selected.append(event.job_id)


async def _seed(container: ServiceContainer) -> list[Any]:
    """Two jobs, Initech discovered last, so recency ordering is observable."""
    jobs = [
        _job(source_job_id="1", company="Globex", title="Staff Engineer"),
        _job(source_job_id="2", company="Initech", title="Product Designer"),
    ]
    async with container.repositories() as repos:
        for job in jobs:
            await repos.jobs.upsert(job)
    return jobs


async def _settle(pilot: Pilot) -> None:
    """Let the load worker run and the screen repaint."""
    for _ in range(6):
        await pilot.pause()


def _table(screen: JobsScreen) -> DataTable:
    return screen.query_one("#jobs-table", DataTable)


def _companies(screen: JobsScreen) -> list[str]:
    table = _table(screen)
    return [str(table.get_row_at(row)[0]) for row in range(table.row_count)]


async def _shown(app: TuiApp, pilot: Pilot) -> JobsScreen:
    await app.push_screen(JobsScreen())
    await _settle(pilot)
    screen = app.screen
    assert isinstance(screen, JobsScreen)
    return screen


async def test_the_table_lists_seeded_jobs_with_their_companies(container: ServiceContainer) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        assert _table(screen).row_count == 2
        assert sorted(_companies(screen)) == ["Globex", "Initech"]


async def test_each_row_shows_its_title_company_and_location(container: ServiceContainer) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        table = _table(screen)
        labels = [str(column.label) for column in table.columns.values()]
        assert labels == ["Company", "Title", "Location", "Score", "Recommendation", "State"]

        row = [str(cell) for cell in table.get_row_at(0)]
        assert row[0] == "Initech"
        assert row[1] == "Product Designer"


async def test_an_unscored_job_shows_a_placeholder_not_a_fake_zero(container: ServiceContainer) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        row = [str(cell) for cell in _table(screen).get_row_at(0)]
        assert row[3] == "—"
        assert row[4] == "—"
        assert row[5] == "—"


async def test_no_jobs_shows_an_empty_state_rather_than_a_blank_table(
    container: ServiceContainer,
) -> None:
    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        assert _table(screen).row_count == 0
        empty = screen.query_one("#empty-state")
        assert empty.display is True
        assert "no jobs" in str(empty.renderable).lower()


async def test_rows_are_present_so_the_empty_state_is_hidden(container: ServiceContainer) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        assert screen.query_one("#empty-state").display is False


async def test_typing_in_the_search_box_narrows_the_rows(container: ServiceContainer) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)
        assert _table(screen).row_count == 2

        screen.query_one("#search").value = "globex"
        await _settle(pilot)

        assert _table(screen).row_count == 1
        assert _companies(screen) == ["Globex"]


async def test_the_search_also_matches_titles(container: ServiceContainer) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        screen.query_one("#search").value = "designer"
        await _settle(pilot)

        assert _companies(screen) == ["Initech"]


async def test_clearing_the_search_lists_everything_again(container: ServiceContainer) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        screen.query_one("#search").value = "globex"
        await _settle(pilot)
        assert _table(screen).row_count == 1

        screen.query_one("#search").value = ""
        await _settle(pilot)
        assert _table(screen).row_count == 2


async def test_a_search_that_matches_nothing_is_an_empty_state_not_an_error(
    container: ServiceContainer,
) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        screen.query_one("#search").value = "nothingmatchesthis"
        await _settle(pilot)

        assert _table(screen).row_count == 0
        empty = screen.query_one("#empty-state")
        assert empty.display is True
        assert "nothingmatchesthis" in str(empty.renderable)


@pytest.mark.parametrize("blank", ["", "   "])
async def test_a_blank_search_is_treated_as_no_filter(
    container: ServiceContainer, blank: str
) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        screen.query_one("#search").value = blank
        await _settle(pilot)

        assert _table(screen).row_count == 2


async def test_pressing_enter_on_a_row_reports_that_jobs_id(container: ServiceContainer) -> None:
    jobs = await _seed(container)

    app = _RecordingApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        _table(screen).focus()
        await pilot.press("enter")
        await _settle(pilot)

        assert app.selected == [jobs[1].id]


async def test_select_row_returns_the_id_under_the_cursor(container: ServiceContainer) -> None:
    jobs = await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        assert screen.select_row() == jobs[1].id


async def test_select_row_on_an_empty_table_reports_nothing(container: ServiceContainer) -> None:
    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        assert screen.select_row() is None


async def test_the_status_line_counts_what_is_shown(container: ServiceContainer) -> None:
    await _seed(container)

    app = TuiApp(container)
    async with app.run_test() as pilot:
        screen = await _shown(app, pilot)

        assert "2" in str(screen.query_one("#status").renderable)

        screen.query_one("#search").value = "globex"
        await _settle(pilot)
        assert "1" in str(screen.query_one("#status").renderable)
