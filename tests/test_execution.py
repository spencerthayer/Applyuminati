"""HITL, ApplicationAttempt persistence, inbox, and task BLOCKED handling."""

from __future__ import annotations

from typing import Any, cast

from applyuminati.applications.detect import detect_job
from applyuminati.applications.driver import DriverContext, DriverOutcome, DriverOutcomeKind
from applyuminati.browser.base import BrowserSession
from applyuminati.browser.host_manager import BrowserHostManager, LiveHost
from applyuminati.core.errors import FailureCategory, NeedsHumanError
from applyuminati.core.logging import get_logger
from applyuminati.core.models.browser_host import BrowserHostRecord
from applyuminati.core.models.execution import (
    ApplicationAttempt,
    InterventionReason,
    InterventionResolution,
    WorkflowState,
)
from applyuminati.core.models.job import AtsVendor, Job, SourceTier
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.task import TaskState
from applyuminati.core.settings import ExecutionMode
from applyuminati.db.repositories.attempts import AttemptRepository
from applyuminati.db.repositories.jobs import JobRepository
from applyuminati.db.repositories.tasks import TaskRepository
from applyuminati.plugins.applications.greenhouse import GreenhouseDriver
from applyuminati.services.attempt_service import AttemptService, host_presence
from applyuminati.services.attempt_tasks import (
    APPLICATION_ATTEMPT_KIND,
    ApplicationAttemptPayload,
    run_application_attempt,
)
from applyuminati.services.container import Repositories
from applyuminati.sources.normalize import build_job
from applyuminati.tasks.handlers import TaskContext
from applyuminati.tasks.queue import TaskQueue


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


async def test_waiting_for_human_is_not_a_failure() -> None:
    attempt = ApplicationAttempt(
        application_id="a",
        job_id="j",
        driver="greenhouse",
    )
    intervention = attempt.open_intervention(
        InterventionReason.AUTHENTICATION_REQUIRED, "Sign into Acme"
    )
    assert attempt.workflow_state is WorkflowState.WAITING_FOR_HUMAN
    assert intervention.open
    assert attempt.workflow_state not in {WorkflowState.FAILED, WorkflowState.CANCELLED}


def test_handoff_override_can_keep_the_agent_in_control() -> None:
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="greenhouse")
    intervention = attempt.open_intervention(
        InterventionReason.CAPTCHA_REQUIRED,
        "This challenge is actually a form question",
        requires_browser_handoff=False,
    )
    assert intervention.requires_browser_handoff is False


async def test_keep_control_does_not_resume(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1",
            job=job,
            profile=CareerProfile(),
            mode=ExecutionMode.FILL_NO_SUBMIT,
        )
        attempt.open_intervention(InterventionReason.CAPTCHA_REQUIRED, "Complete the challenge")
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        kept = await service.resolve(attempt.id, opened.id, InterventionResolution.KEEP_CONTROL)
        assert kept.workflow_state is WorkflowState.WAITING_FOR_HUMAN
        assert kept.pending_intervention is not None
        queued = await repos.tasks.list()
        assert [task for task in queued if task.kind == APPLICATION_ATTEMPT_KIND] == []


async def test_done_continue_releases_the_pause(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.open_intervention(InterventionReason.MFA_REQUIRED, "Complete MFA")
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        resumed = await service.resolve(attempt.id, opened.id, InterventionResolution.DONE_CONTINUE)
        assert resumed.workflow_state is WorkflowState.PENDING
        assert resumed.pending_intervention is None
        queued = await repos.tasks.list()
        attempt_tasks = [task for task in queued if task.kind == APPLICATION_ATTEMPT_KIND]
        assert len(attempt_tasks) == 1
        assert attempt_tasks[0].payload == {"attempt_id": attempt.id}
        again = await service.resolve(attempt.id, opened.id, InterventionResolution.DONE_CONTINUE)
        assert again.workflow_state is WorkflowState.PENDING
        queued_again = await repos.tasks.list()
        assert len([task for task in queued_again if task.kind == APPLICATION_ATTEMPT_KIND]) == 1


async def test_skip_cancels_the_attempt(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.open_intervention(InterventionReason.AUTHENTICATION_REQUIRED, "Sign in")
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        skipped = await service.resolve(
            attempt.id, opened.id, InterventionResolution.SKIP_APPLICATION
        )
        assert skipped.workflow_state is WorkflowState.CANCELLED


async def test_attempt_round_trips_checkpoints(database) -> None:
    attempt = ApplicationAttempt(
        application_id="app",
        job_id="job",
        driver="greenhouse",
        task_space_id="applyuminati:app",
    )
    attempt.record_checkpoint("application_opened", url="https://example.com")
    async with database.session() as session:
        repo = AttemptRepository(session)
        await repo.save(attempt)
        loaded = await repo.get(attempt.id)
    assert loaded is not None
    assert loaded.task_space_id == "applyuminati:app"
    assert loaded.latest_checkpoint is not None
    assert loaded.latest_checkpoint.kind == "application_opened"


async def test_inbox_lists_only_open_interventions(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.open_intervention(
            InterventionReason.AMBIGUOUS_QUESTION,
            'Question needs an answer: "Are you willing to travel?"',
            requires_browser_handoff=False,
            question_text="Are you willing to travel?",
        )
        await repos.attempts.save(attempt)
        inbox = await service.inbox()
    assert len(inbox) == 1
    assert inbox[0].company == "Acme"
    assert inbox[0].intervention.requires_browser_handoff is False


async def test_needs_human_blocks_instead_of_failing(database) -> None:
    async with database.session() as session:
        queue = TaskQueue(TaskRepository(session))
        await queue.submit("application.execute", {"attempt_id": "x"})
        claimed = await queue.claim()
        assert claimed is not None
        failed = await queue.fail(claimed, NeedsHumanError("sign in"))
        assert failed.state is TaskState.BLOCKED
        assert failed.failure_category is FailureCategory.NEEDS_HUMAN
        requeued = await queue.requeue(failed)
        assert requeued.state is TaskState.PENDING
        assert requeued.attempt_count == failed.attempt_count


def test_greenhouse_url_is_detected_from_a_linkedin_job() -> None:
    job = _job()
    detection = detect_job(job)
    assert detection.ats is AtsVendor.GREENHOUSE


async def test_two_attempts_for_one_application_get_distinct_task_spaces(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        first = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        second = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
    assert first.task_space_id == f"applyuminati:{first.id}"
    assert second.task_space_id == f"applyuminati:{second.id}"
    assert first.task_space_id != second.task_space_id


class _Conn:
    async def send_text(self, payload: str) -> None:
        return None

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        return None


class _PausedDriver:
    metadata = GreenhouseDriver().metadata

    def detects(self, url: str):
        return GreenhouseDriver().detects(url)

    async def run(
        self,
        attempt: ApplicationAttempt,
        session: BrowserSession,
        context: DriverContext,
    ) -> DriverOutcome:
        attempt.open_intervention(InterventionReason.CAPTCHA_REQUIRED, "Solve the challenge")
        return DriverOutcome(kind=DriverOutcomeKind.WAITING_FOR_HUMAN, attempt=attempt)


async def _session_factory(_attempt: ApplicationAttempt) -> BrowserSession:
    return cast(BrowserSession, object())


async def test_attempt_handler_releases_the_queue_on_human_pause(database) -> None:
    job = _job()

    async def _noop_checkpoint(state: dict[str, Any]) -> None:
        return None

    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.open_intervention(InterventionReason.MFA_REQUIRED, "Complete MFA")
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        await service.resolve(attempt.id, opened.id, InterventionResolution.DONE_CONTINUE)
        queue = TaskQueue(repos.tasks)
        claimed = await queue.claim(kinds=[APPLICATION_ATTEMPT_KIND])
        assert claimed is not None
        prior_attempts = claimed.attempt_count
        result = await run_application_attempt(
            ApplicationAttemptPayload(attempt_id=attempt.id),
            TaskContext(
                task_id=claimed.id,
                kind=APPLICATION_ATTEMPT_KIND,
                run_id=None,
                attempt=1,
                strategy=None,
                resume_state={},
                logger=get_logger(__name__),
                checkpoint_sink=_noop_checkpoint,
            ),
            repos=repos,
            driver=_PausedDriver(),
            session_factory=_session_factory,
        )
        assert result["status"] == WorkflowState.WAITING_FOR_HUMAN.value
        loaded = await repos.attempts.get(attempt.id)
        assert loaded is not None
        assert loaded.workflow_state is WorkflowState.WAITING_FOR_HUMAN
        finished = await queue.complete(claimed, result)
        assert finished.state is TaskState.SUCCEEDED
        assert finished.attempt_count == prior_attempts


async def test_inbox_reports_live_host_presence(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1",
            job=job,
            profile=None,
            mode=ExecutionMode.FILL_NO_SUBMIT,
            browser_host_id="mac",
        )
        intervention = attempt.open_intervention(
            InterventionReason.AUTHENTICATION_REQUIRED, "Sign in"
        )
        await repos.attempts.save(attempt)
        manager = BrowserHostManager()
        assert host_presence(attempt, intervention, manager).value == "offline"
        live_record = BrowserHostRecord(host_id="mac")
        manager._hosts["mac"] = LiveHost(record=live_record, connection=_Conn())
        assert host_presence(attempt, intervention, manager).value == "connected"
