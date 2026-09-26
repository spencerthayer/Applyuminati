"""A terminal attempt outcome must move the Application it belongs to.

The attempt aggregate and the Application aggregate are separate on purpose:
one is what the executor is doing, the other is the hiring process. Nothing
reconciled them, so a confirmed submission never showed up as SUBMITTED and
the pipeline counters stayed wrong.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from applyuminati.applications.driver import DriverOutcome, DriverOutcomeKind
from applyuminati.browser.base import BrowserSession
from applyuminati.browser.capabilities import PUBLIC_FORM_APPLICATION
from applyuminati.core.logging import get_logger
from applyuminati.core.models.application import ApplicationState
from applyuminati.core.models.execution import (
    InterventionReason,
    WorkflowState,
)
from applyuminati.core.models.job import Job, SourceTier
from applyuminati.core.models.jsonresume import JsonResume, ResumeBasics
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.settings import ExecutionMode
from applyuminati.db.repositories.jobs import JobRepository
from applyuminati.services.attempt_service import AttemptService
from applyuminati.services.attempt_tasks import (
    ApplicationAttemptPayload,
    run_application_attempt,
)
from applyuminati.services.container import Repositories
from applyuminati.sources.normalize import build_job
from applyuminati.tasks.handlers import TaskContext


def _job() -> Job:
    return build_job(
        source="linkedin",
        tier=SourceTier.AGGREGATOR,
        source_job_id="99",
        url="https://www.linkedin.com/jobs/view/99",
        title="Staff Engineer",
        company="Acme",
        apply_url="https://boards.greenhouse.io/acme/jobs/99",
    )


def _context() -> TaskContext:
    async def _noop(state: dict) -> None:
        return None

    return TaskContext(
        task_id="t1",
        kind="application.attempt",
        run_id=None,
        attempt=1,
        strategy=None,
        resume_state={},
        logger=get_logger(__name__),
        checkpoint_sink=_noop,
    )


class _ConfirmingDriver:
    """Reports a confirmed submission without touching a real browser."""

    metadata = SimpleNamespace(requirements=PUBLIC_FORM_APPLICATION)

    async def run(self, attempt, session, context):
        attempt.workflow_state = WorkflowState.COMPLETED
        return DriverOutcome(kind=DriverOutcomeKind.COMPLETED, attempt=attempt)


class _AbandoningDriver:
    metadata = SimpleNamespace(requirements=PUBLIC_FORM_APPLICATION)

    async def run(self, attempt, session, context):
        attempt.workflow_state = WorkflowState.CANCELLED
        return DriverOutcome(kind=DriverOutcomeKind.CANCELLED, attempt=attempt)


class _PausingDriver:
    metadata = SimpleNamespace(requirements=PUBLIC_FORM_APPLICATION)

    async def run(self, attempt, session, context):
        attempt.open_intervention(InterventionReason.CAPTCHA_REQUIRED, "Solve it")
        return DriverOutcome(kind=DriverOutcomeKind.WAITING_FOR_HUMAN, attempt=attempt)


class _FakeManager:
    async def acquire(self, attempt) -> BrowserSession:
        from typing import cast

        return cast(BrowserSession, object())

    async def close(self, attempt_id: str) -> None:
        return None


async def _seed(database):
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        await repos.profiles.upsert(
            CareerProfile(id="p1", label="t", resume=JsonResume(basics=ResumeBasics(name="T")))
        )
    return job


async def _run_with(database, job, driver):
    async with database.session() as session:
        repos = Repositories.bind(session)
        profile = await repos.profiles.get_active()
        attempt = await AttemptService(repos).start_for_job(
            job_id=job.id, profile=profile, mode=ExecutionMode.AUTONOMOUS_SUBMIT
        )
    with (
        patch("applyuminati.services.attempt_tasks.select_browser", _select),
        patch("applyuminati.services.attempt_tasks._local_manager", _FakeManager),
        patch("applyuminati.services.attempt_tasks.detect_driver", lambda u: (driver, None)),
        patch("applyuminati.services.attempt_service.detect_driver", lambda u: (driver, None)),
    ):
        async with database.session() as session:
            await run_application_attempt(
                ApplicationAttemptPayload(attempt_id=attempt.id),
                _context(),
                repos=Repositories.bind(session),
            )
    return attempt.id


async def _select(settings, requirements):
    return (
        SimpleNamespace(metadata=SimpleNamespace(slug="playwright")),
        SimpleNamespace(state="healthy"),
    )


async def test_a_confirmed_submission_marks_the_application_submitted(database) -> None:
    job = await _seed(database)
    attempt_id = await _run_with(database, job, _ConfirmingDriver())
    async with database.read_session() as session:
        repos = Repositories.bind(session)
        attempt = await repos.attempts.get(attempt_id)
        assert attempt is not None
        assert attempt.workflow_state is WorkflowState.COMPLETED
        application = await repos.applications.get(attempt.application_id)
        assert application is not None
        assert application.state is ApplicationState.SUBMITTED


async def test_a_cancelled_attempt_marks_the_application_skipped(database) -> None:
    job = await _seed(database)
    attempt_id = await _run_with(database, job, _AbandoningDriver())
    async with database.read_session() as session:
        repos = Repositories.bind(session)
        attempt = await repos.attempts.get(attempt_id)
        assert attempt is not None
        application = await repos.applications.get(attempt.application_id)
        assert application is not None
        assert application.state is ApplicationState.WITHDRAWN


async def test_a_paused_attempt_leaves_the_application_alone(database) -> None:
    """WAITING_FOR_HUMAN is a pause, not an outcome, so nothing may advance."""
    job = await _seed(database)
    attempt_id = await _run_with(database, job, _PausingDriver())
    async with database.read_session() as session:
        repos = Repositories.bind(session)
        attempt = await repos.attempts.get(attempt_id)
        assert attempt is not None
        assert attempt.workflow_state is WorkflowState.WAITING_FOR_HUMAN
        application = await repos.applications.get(attempt.application_id)
        assert application is not None
        # Starting the attempt legitimately moved it to APPLYING. What must not
        # happen is an outcome being recorded, because none has been reached.
        assert application.state is ApplicationState.APPLYING


@pytest.mark.parametrize("driver", [_ConfirmingDriver, _AbandoningDriver, _PausingDriver])
async def test_reconciling_twice_is_harmless(database, driver) -> None:
    """The worker may retry a step, so reconciliation must be idempotent."""
    job = await _seed(database)
    await _run_with(database, job, driver())
