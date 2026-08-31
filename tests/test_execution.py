"""HITL, ApplicationAttempt persistence, inbox, and task BLOCKED handling."""

from __future__ import annotations

from applyuminati.applications.detect import detect_job
from applyuminati.core.errors import FailureCategory, NeedsHumanError
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
from applyuminati.services.attempt_service import AttemptService
from applyuminati.services.container import Repositories
from applyuminati.sources.normalize import build_job
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
