"""The Job Detail screen must start an application, and refuse to start two.

These assertions are about what the screen shows the user, not about whether a
widget exists: pressing Apply has to produce one attempt row and one queued
task, a second press has to say *why* it did nothing, and a job that has gone
away has to say so rather than fail silently.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="the tui extra is not installed")

from pathlib import Path

from textual.widgets import Static

from applyuminati.core.models.application import Application
from applyuminati.core.models.common import Location
from applyuminati.core.models.job import Job, SourceTier
from applyuminati.core.models.jsonresume import JsonResume, ResumeBasics
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.scoring import (
    BlockerSeverity,
    DimensionScore,
    FitScore,
    MissingRequirement,
    Recommendation,
    ScoreDimension,
)
from applyuminati.services.attempt_tasks import APPLICATION_ATTEMPT_KIND
from applyuminati.sources.normalize import build_job
from applyuminati.tui import app as tui_app
from applyuminati.tui.app import TuiApp

#: Bounded wait for the async apply worker. Fails loudly rather than hanging.
_WAIT_ROUNDS = 200


def _job() -> Job:
    return build_job(
        source="linkedin",
        tier=SourceTier.AGGREGATOR,
        source_job_id="99",
        url="https://www.linkedin.com/jobs/view/99",
        title="Staff Engineer",
        company="Acme",
        apply_url="https://boards.greenhouse.io/acme/jobs/99",
        description="Build distributed systems.\nRequirements: 5 years Go, Kubernetes.",
        locations=[Location(city="Berlin", country="Germany")],
    )


def _profile() -> CareerProfile:
    return CareerProfile(id="p1", label="test", resume=JsonResume(basics=ResumeBasics(name="Test")))


def _score(job_id: str) -> FitScore:
    return FitScore(
        job_id=job_id,
        profile_id="p1",
        overall=0.72,
        recommendation=Recommendation.APPLY,
        dimensions=[
            DimensionScore(
                dimension=ScoreDimension.REQUIRED_SKILLS,
                score=0.8,
                weight=0.4,
                rationale="4 of 5 required skills evidenced",
            ),
            DimensionScore(
                dimension=ScoreDimension.DEMONSTRATED_EXPERIENCE,
                score=0.6,
                weight=0.3,
                rationale="2 of 4 relevant years",
            ),
        ],
        missing_requirements=[
            MissingRequirement(requirement="Kubernetes", severity=BlockerSeverity.HARD),
            MissingRequirement(requirement="Go", severity=BlockerSeverity.MINOR),
        ],
        explanation="Strong systems overlap, no Go evidence.",
    )


async def _seed_job(container, *, with_profile: bool = True) -> Job:
    job = _job()
    async with container.repositories() as repos:
        await repos.jobs.upsert(job)
        if with_profile:
            await repos.profiles.upsert(_profile())
            await repos.scores.add(_score(job.id))
    return job


async def _seed(container) -> tuple[Job, Application]:
    """A job, an active profile, a fit score, and an Application row."""
    job = await _seed_job(container)
    async with container.repositories() as repos:
        application = await repos.applications.ensure(job.id, "p1")
    return job, application


def _text(widget: Static) -> str:
    content = widget.content
    return content if isinstance(content, str) else str(content)


def _status(screen) -> str:
    return _text(screen.query_one("#status", Static))


async def _settle(pilot, screen, *, want: str | None = None) -> str:
    """Let the apply worker run to completion, then hand back the status line.

    ``pilot.pause()`` hands the event loop back to the worker between rounds,
    which is what the worker needs to finish its database round trips.
    """
    status = ""
    for _ in range(_WAIT_ROUNDS):
        await pilot.pause()
        status = _status(screen)
        if want is not None and want in status:
            return status
        if want is None and status:
            return status
    expected = f"containing {want!r}" if want is not None else "any text"
    raise AssertionError(f"status line never settled to a value {expected}; last was {status!r}")


class _ScreenHost(TuiApp):
    """TuiApp with the background attempt worker suppressed.

    The worker would claim the task this test just queued and go looking for a
    real browser. Queuing is what the screen owns; running the attempt is not.
    """

    # Resolved absolutely: a subclass defined in this module would otherwise
    # look for tests/styles.tcss.
    CSS_PATH = str(Path(tui_app.__file__).parent / "styles.tcss")

    async def on_mount(self) -> None:
        self.query_one("#placeholder", Static).update("ready")


def _host_app(container) -> _ScreenHost:
    return _ScreenHost(container)


async def _render(container, job_id: str):
    from applyuminati.tui.screens.job_detail import JobDetailScreen

    screen = JobDetailScreen(job_id=job_id)
    app = _host_app(container)
    return app, screen


async def test_the_detail_pane_shows_the_job_the_score_and_the_mode(container, settings) -> None:
    job, _ = await _seed(container)
    app, screen = await _render(container, job.id)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        pane = screen.query_one("#detail-pane")
        assert pane is not None
        body = _text(screen.query_one("#detail-body", Static))

    assert "Staff Engineer" in body
    assert "Acme" in body
    assert "Berlin, Germany" in body
    assert "https://boards.greenhouse.io/acme/jobs/99" in body
    assert "72" in body  # the overall fit score
    assert "required_skills" in body  # one line per dimension
    assert "4 of 5 required skills evidenced" in body
    assert "Kubernetes" in body  # missing requirements
    assert "Build distributed systems." in body  # the description
    # The default mode submits without asking, so it has to be on screen.
    assert settings.execution_mode.value in body


async def test_apply_creates_exactly_one_attempt_and_one_task(container) -> None:
    job, application = await _seed(container)
    app, screen = await _render(container, job.id)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.press("a")
        status = await _settle(pilot, screen, want="attempt")
        async with container.read_repositories() as repos:
            attempts = await repos.attempts.list_for_application(application.id)
            queued = await repos.tasks.list()
            tasks = [task for task in queued if task.kind == APPLICATION_ATTEMPT_KIND]

    assert len(attempts) == 1, attempts
    assert len(tasks) == 1, tasks
    assert tasks[0].payload == {"attempt_id": attempts[0].id}
    assert attempts[0].id in status
    assert "Needs You" in status


async def test_applying_twice_is_refused_with_a_specific_message(container) -> None:
    job, application = await _seed(container)
    app, screen = await _render(container, job.id)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.press("a")
        await _settle(pilot, screen, want="attempt")
        await pilot.press("a")
        status = await _settle(pilot, screen, want="already")
        async with container.read_repositories() as repos:
            attempts = await repos.attempts.list_for_application(application.id)

    assert len(attempts) == 1, attempts
    assert "in progress" in status


async def test_applying_without_a_profile_says_to_import_one(container) -> None:
    job = await _seed_job(container, with_profile=False)
    app, screen = await _render(container, job.id)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.press("a")
        status = await _settle(pilot, screen, want="profile")

    assert "import a career profile" in status


async def test_an_unknown_job_reports_not_found(container) -> None:
    app, screen = await _render(container, "01JDOESNOTEXIST")
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.press("a")
        await _settle(pilot, screen, want="not found")
        body = _text(screen.query_one("#detail-body", Static))

    assert "01JDOESNOTEXIST" in body
    assert "not found" in body.lower()
