"""The dashboard screen must show what ``applyuminati status`` shows.

The CLI's ``status`` command prints six counts, a breakdown by recommendation
and the latest run. If the screen shows a different number from the CLI, one of
them is lying, so these tests seed a known database and read the rendered
content rather than the widget tree.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest

pytest.importorskip("textual", reason="the tui extra is not installed")

from textual.widgets import Label, Static

from applyuminati.core.models.application import Application, ApplicationState
from applyuminati.core.models.job import Job, SourceTier
from applyuminati.core.models.jsonresume import JsonResume, ResumeBasics
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.scoring import FitScore, Recommendation
from applyuminati.core.models.task import RunRecord, RunState
from applyuminati.db.repositories.jobs import JobRepository
from applyuminati.db.session import Database
from applyuminati.services.container import Repositories, ServiceContainer
from applyuminati.services.dashboard_service import DashboardService
from applyuminati.sources.normalize import build_job
from applyuminati.tui.app import TuiApp
from applyuminati.tui.screens.dashboard import DashboardScreen

PROFILE_ID = "p1"

_APPLICATION_STATES = [
    ApplicationState.SHORTLISTED,
    ApplicationState.READY,
    ApplicationState.SUBMITTED,
    ApplicationState.NEEDS_ATTENTION,
]

_RECOMMENDATIONS = [
    Recommendation.APPLY,
    Recommendation.APPLY,
    Recommendation.INVESTIGATE,
    Recommendation.SKIP,
]


def _job(source_job_id: str) -> Job:
    return build_job(
        source="linkedin",
        tier=SourceTier.AGGREGATOR,
        source_job_id=source_job_id,
        url=f"https://www.linkedin.com/jobs/view/{source_job_id}",
        title=f"Engineer {source_job_id}",
        company="Acme",
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{source_job_id}",
    )


async def _seed(database: Database, *, job_count: int = 5) -> None:
    """A pipeline mid-flight: applications, scores and one finished run."""
    jobs = [_job(str(index)) for index in range(job_count)]
    async with database.session() as session:
        job_repo = JobRepository(session)
        repos = Repositories.bind(session)
        await repos.profiles.upsert(
            CareerProfile(
                id=PROFILE_ID,
                label="test",
                resume=JsonResume(basics=ResumeBasics(name="Test")),
            )
        )
        for job in jobs:
            await job_repo.upsert(job)
        for job, state in zip(jobs, _APPLICATION_STATES, strict=False):
            await repos.applications.save(
                Application(job_id=job.id, profile_id=PROFILE_ID, state=state)
            )
        for job, recommendation in zip(jobs, _RECOMMENDATIONS, strict=False):
            await repos.scores.add(
                FitScore(
                    job_id=job.id,
                    profile_id=PROFILE_ID,
                    overall=0.5,
                    recommendation=recommendation,
                )
            )
        await repos.runs.create(
            RunRecord(
                kind="discovery",
                state=RunState.SUCCEEDED,
                stats={"jobs_discovered": job_count},
            )
        )


def _tile(screen: DashboardScreen, tile_id: str) -> str:
    """The number the tile is rendering."""
    return str(screen.query_one(f"#{tile_id} .stat-value", Label).content)


def _text(screen: DashboardScreen, selector: str) -> str:
    return str(screen.query_one(selector, Static).content)


async def _settle(pilot, screen: DashboardScreen) -> None:
    """Let the load worker finish; it hides the loading line when it has."""
    loading = screen.query_one("#dashboard-loading", Static)
    for _ in range(200):
        if not loading.display:
            return
        await pilot.pause()


@asynccontextmanager
async def _dashboard(container: ServiceContainer) -> AsyncIterator[DashboardScreen]:
    """Run the screen headlessly against ``container`` and wait for its worker."""
    app = TuiApp(container)
    async with app.run_test() as pilot:
        await app.push_screen(DashboardScreen())
        # App.query_one searches the app's own DOM, not the pushed screen.
        screen = cast("DashboardScreen", app.screen)
        await _settle(pilot, screen)
        yield screen


async def test_the_dashboard_counts_match_the_service(container, database) -> None:
    await _seed(database)
    async with _dashboard(container) as screen:
        async with container.read_repositories() as repos:
            view = await DashboardService(repos).build()

        assert _tile(screen, "stat-total-jobs") == str(view.total_jobs)
        assert _tile(screen, "stat-shortlisted") == str(view.shortlisted)
        assert _tile(screen, "stat-ready") == str(view.ready)
        assert _tile(screen, "stat-submitted") == str(view.submitted)
        assert _tile(screen, "stat-needs-attention") == str(view.needs_attention)
        # The CLI prints "Scored: {scored} / {total_jobs}", so the tile does too.
        assert _tile(screen, "stat-scored") == f"{view.scored} / {view.total_jobs}"

        assert _tile(screen, "stat-total-jobs") == "5"
        assert _tile(screen, "stat-shortlisted") == "1"
        assert _tile(screen, "stat-ready") == "1"
        assert _tile(screen, "stat-submitted") == "1"
        assert _tile(screen, "stat-needs-attention") == "1"
        assert _tile(screen, "stat-scored") == "4 / 5"


async def test_the_dashboard_breaks_down_recommendations_and_shows_the_latest_run(
    container, database
) -> None:
    await _seed(database)
    async with _dashboard(container) as screen:
        breakdown = _text(screen, "#recommendation-breakdown")
        assert "apply: 2" in breakdown
        assert "investigate: 1" in breakdown
        assert "skip: 1" in breakdown

        latest_run = _text(screen, "#latest-run")
        assert "discovery" in latest_run
        assert RunState.SUCCEEDED.value in latest_run
        assert "jobs_discovered" in latest_run


async def test_an_empty_database_renders_zeros_and_says_so(container) -> None:
    async with _dashboard(container) as screen:
        for tile_id in (
            "stat-total-jobs",
            "stat-shortlisted",
            "stat-ready",
            "stat-submitted",
            "stat-needs-attention",
        ):
            assert _tile(screen, tile_id) == "0", tile_id
        assert _tile(screen, "stat-scored") == "0 / 0"
        assert "none yet" in _text(screen, "#recommendation-breakdown")
        assert "none yet" in _text(screen, "#latest-run")
        assert screen.query_one("#dashboard-error", Static).display is False


async def test_refreshing_picks_up_new_jobs(container, database) -> None:
    app = TuiApp(container)
    async with app.run_test() as pilot:
        await app.push_screen(DashboardScreen())
        screen = cast("DashboardScreen", app.screen)
        await _settle(pilot, screen)
        assert _tile(screen, "stat-total-jobs") == "0"

        await _seed(database, job_count=1)
        await pilot.press("r")
        for _ in range(200):
            await pilot.pause()
            if _tile(screen, "stat-total-jobs") == "1":
                break
        assert _tile(screen, "stat-total-jobs") == "1"
