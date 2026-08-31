"""Durable HITL for application attempts.

Entering WAITING_FOR_HUMAN persists the attempt and releases the worker.
Resume requeues from the checkpoint after an explicit user resolution.
``keep_control`` leaves the attempt paused; no timer reclaims the browser.
"""

from __future__ import annotations

from typing import Any

from applyuminati.applications.driver import DriverContext, DriverOutcomeKind, detect_driver
from applyuminati.applications.detect import detect_job
from applyuminati.browser.base import BrowserSession
from applyuminati.core.clock import utcnow
from applyuminati.core.errors import NotFoundError
from applyuminati.core.logging import get_logger
from applyuminati.core.models.execution import (
    ApplicationAttempt,
    AttemptEventKind,
    HumanIntervention,
    InterventionResolution,
    WorkflowState,
)
from applyuminati.core.models.job import Job
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.questionnaire import AnswerStatus
from applyuminati.core.settings import ExecutionMode
from applyuminati.services.container import Repositories

log = get_logger(__name__)

__all__ = ["AttemptService", "InboxItem"]


class InboxItem:
    def __init__(
        self,
        attempt: ApplicationAttempt,
        intervention: HumanIntervention,
        *,
        company: str | None = None,
        title: str | None = None,
    ) -> None:
        self.attempt = attempt
        self.intervention = intervention
        self.company = company
        self.title = title


class AttemptService:
    def __init__(self, repos: Repositories) -> None:
        self._repos = repos

    async def create(
        self,
        *,
        application_id: str,
        job: Job,
        profile: CareerProfile | None,
        mode: ExecutionMode,
        browser_host_id: str | None = None,
    ) -> ApplicationAttempt:
        detection = detect_job(job)
        driver_name = detection.ats.value
        matched = detect_driver(job.apply_url or job.canonical_url)
        if matched is not None:
            driver_name = matched[0].metadata.slug
        attempt = ApplicationAttempt(
            application_id=application_id,
            job_id=job.id,
            profile_id=profile.id if profile else None,
            driver=driver_name,
            submission_mode=mode,
            browser_host_id=browser_host_id,
            task_space_id=f"applyuminati:{application_id}",
        )
        await self._repos.attempts.save(attempt)
        log.info(
            "attempt.created",
            attempt_id=attempt.id,
            application_id=application_id,
            driver=driver_name,
            ats=detection.ats.value,
        )
        return attempt

    async def get(self, attempt_id: str) -> ApplicationAttempt:
        record = await self._repos.attempts.get(attempt_id)
        if record is None:
            raise NotFoundError(f"attempt {attempt_id} not found", code="resource_gone.attempt")
        return record

    async def inbox(self) -> list[InboxItem]:
        waiting = await self._repos.attempts.list_waiting()
        items: list[InboxItem] = []
        for attempt in waiting:
            intervention = attempt.open_intervention
            if intervention is None:
                continue
            job = await self._repos.jobs.get(attempt.job_id)
            items.append(
                InboxItem(
                    attempt,
                    intervention,
                    company=job.company if job else None,
                    title=job.title if job else None,
                )
            )
        return items

    async def resolve(
        self,
        attempt_id: str,
        intervention_id: str,
        resolution: InterventionResolution,
        *,
        payload: dict[str, Any] | None = None,
    ) -> ApplicationAttempt:
        attempt = await self.get(attempt_id)
        intervention = next((item for item in attempt.interventions if item.id == intervention_id), None)
        if intervention is None:
            raise NotFoundError(
                f"intervention {intervention_id} not found", code="resource_gone.intervention"
            )
        if resolution is InterventionResolution.KEEP_CONTROL:
            intervention.resolve(resolution, payload=payload)
            attempt.record_event(
                AttemptEventKind.INTERVENTION_RESOLVED,
                "user is keeping browser control",
                resolution=resolution.value,
            )
            # Still waiting. The agent must not reclaim.
            attempt.workflow_state = WorkflowState.WAITING_FOR_HUMAN
            await self._repos.attempts.save(attempt)
            return attempt
        if resolution is InterventionResolution.SKIP_APPLICATION:
            intervention.resolve(resolution, payload=payload)
            attempt.workflow_state = WorkflowState.CANCELLED
            attempt.completed_at = utcnow()
            attempt.record_event(AttemptEventKind.CANCELLED, "user skipped the application")
            await self._repos.attempts.save(attempt)
            return attempt
        if resolution is InterventionResolution.CANCEL:
            intervention.resolve(resolution, payload=payload)
            attempt.workflow_state = WorkflowState.CANCELLED
            attempt.completed_at = utcnow()
            attempt.record_event(AttemptEventKind.CANCELLED, "user cancelled the attempt")
            await self._repos.attempts.save(attempt)
            return attempt

        if resolution is InterventionResolution.ANSWER and payload and payload.get("answer"):
            key = intervention.question_key
            for draft in attempt.answers:
                if draft.question_key == key:
                    draft.answer = str(payload["answer"])
                    draft.status = AnswerStatus.USER_PROVIDED

        intervention.resolve(resolution, payload=payload)
        attempt.workflow_state = WorkflowState.PENDING
        attempt.record_event(
            AttemptEventKind.RESUMED,
            "user returned control",
            resolution=resolution.value,
        )
        await self._repos.attempts.save(attempt)
        return attempt

    async def run_step(
        self,
        attempt: ApplicationAttempt,
        session: BrowserSession,
        context: DriverContext,
    ) -> ApplicationAttempt:
        matched = detect_driver(context.job.apply_url or context.job.canonical_url)
        if matched is None:
            attempt.workflow_state = WorkflowState.FAILED
            await self._repos.attempts.save(attempt)
            return attempt
        driver, _detection = matched
        outcome = await driver.run(attempt, session, context)
        if outcome.kind is DriverOutcomeKind.WAITING_FOR_HUMAN:
            attempt.workflow_state = WorkflowState.WAITING_FOR_HUMAN
        await self._repos.attempts.save(attempt)
        return attempt
