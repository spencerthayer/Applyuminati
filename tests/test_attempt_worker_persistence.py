"""The attempt worker must be able to write while its own task is claimed.

Every other execution test drives ``run_application_attempt`` with an explicit
``repos``, so the handler shares the caller's session and the worker's own
transaction is never in play. Production does the opposite: the registered
handler calls it with no session, so it opens a second connection while the
worker's claim is still open. That is the only path where the two contend.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from applyuminati.applications.driver import DriverOutcome, DriverOutcomeKind
from applyuminati.browser.base import BrowserSession
from applyuminati.browser.capabilities import PUBLIC_FORM_APPLICATION
from applyuminati.core.models.execution import (
    ApplicationAttempt,
    InterventionReason,
    WorkflowState,
)
from applyuminati.core.models.job import Job, SourceTier
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import ExecutionMode
from applyuminati.db.repositories.jobs import JobRepository
from applyuminati.services.attempt_service import AttemptService
from applyuminati.services.attempt_tasks import (
    APPLICATION_ATTEMPT_KIND,
    register_attempt_handlers,
)
from applyuminati.services.container import Repositories, ServiceContainer, set_container
from applyuminati.sources.normalize import build_job
from applyuminati.tasks.queue import TaskQueue
from applyuminati.tasks.worker import TaskWorker


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


class _PausedDriver:
    """Stands in for a real ATS driver so the fake session is never driven.

    The worker path cannot inject a driver, so ``detect_driver`` is patched
    instead. Both call sites read ``(driver, detection)`` from its result.
    """

    metadata = SimpleNamespace(requirements=PUBLIC_FORM_APPLICATION)

    async def run(self, attempt, session, context):
        attempt.open_intervention(InterventionReason.CAPTCHA_REQUIRED, "Solve the challenge")
        return DriverOutcome(kind=DriverOutcomeKind.WAITING_FOR_HUMAN, attempt=attempt)


def _fake_detect(url):
    return (_PausedDriver(), None)


async def _fake_select(settings, requirements):
    selected = SimpleNamespace(metadata=SimpleNamespace(slug="playwright"))
    return selected, HealthReport(plugin="playwright", state=HealthState.HEALTHY, detail="fake")


class _FakeLocalManager:
    def __init__(self) -> None:
        self.acquired: list[str] = []

    async def acquire(self, attempt: ApplicationAttempt) -> BrowserSession:
        self.acquired.append(attempt.id)
        return cast(BrowserSession, object())

    async def close(self, attempt_id: str) -> None:
        return None


async def test_worker_run_once_persists_the_attempt(database, settings) -> None:
    """A claimed task must not hold a write lock that blocks the handler.

    Regression for the observed production failure:
        sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
        [SQL: UPDATE application_attempts SET browser_backend=? ...]
    """
    register_attempt_handlers()
    job = _job()
    fake_manager = _FakeLocalManager()
    # The handler opens its own session through the process container, so that
    # container must point at this test's database or it writes somewhere else.
    set_container(ServiceContainer(settings, database=database))

    try:
        async with database.session() as session:
            await JobRepository(session).upsert(job)
            repos = Repositories.bind(session)
            attempt = await AttemptService(repos).create(
                application_id="app1",
                job=job,
                profile=None,
                mode=ExecutionMode.FILL_NO_SUBMIT,
            )
            await TaskQueue(repos.tasks).submit(
                APPLICATION_ATTEMPT_KIND,
                {"attempt_id": attempt.id},
                idempotency_key=f"test:{attempt.id}",
            )

        with (
            patch("applyuminati.services.attempt_tasks.select_browser", _fake_select),
            patch("applyuminati.services.attempt_tasks._local_manager", lambda: fake_manager),
            patch("applyuminati.services.attempt_tasks.detect_driver", _fake_detect),
            patch("applyuminati.services.attempt_service.detect_driver", _fake_detect),
        ):
            async with database.session() as session:
                queue = TaskQueue(Repositories.bind(session).tasks)
                did_work = await TaskWorker(queue).run_once(kinds=[APPLICATION_ATTEMPT_KIND])
    finally:
        set_container(None)

    assert did_work is True

    async with database.read_session() as session:
        loaded = await AttemptService(Repositories.bind(session)).get(attempt.id)
        assert loaded is not None
        assert loaded.browser_backend == "playwright"
        assert loaded.workflow_state is not WorkflowState.PENDING
