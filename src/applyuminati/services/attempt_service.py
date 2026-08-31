"""Durable HITL for application attempts.

Entering WAITING_FOR_HUMAN persists the attempt and releases the worker.
Resume requeues from the checkpoint after an explicit user resolution.
``keep_control`` leaves the attempt paused; no timer reclaims the browser.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from applyuminati.applications.detect import detect_job
from applyuminati.applications.driver import DriverContext, DriverOutcomeKind, detect_driver
from applyuminati.browser.base import BrowserSession, ControlOwner
from applyuminati.browser.host_manager import BrowserHostManager
from applyuminati.browser.host_protocol import HostCommand
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
from applyuminati.services.hosted_session import HostedBrowserSession
from applyuminati.tasks.queue import TaskQueue

log = get_logger(__name__)

__all__ = [
    "APPLICATION_ATTEMPT_KIND",
    "AttemptService",
    "HostPresence",
    "InboxItem",
    "host_presence",
]

#: Durable queue kind for one application attempt. Payload is the attempt id.
APPLICATION_ATTEMPT_KIND = "application.attempt"

SessionFactory = Callable[[ApplicationAttempt], Awaitable[BrowserSession | None]]


class HostPresence(StrEnum):
    """Live host state as the inbox should show it."""

    CONNECTED = "connected"
    OFFLINE = "offline"
    SESSION_UNAVAILABLE = "session_unavailable"
    NOT_REQUIRED = "not_required"


def host_presence(
    attempt: ApplicationAttempt,
    intervention: HumanIntervention,
    manager: BrowserHostManager | None,
) -> HostPresence:
    """Reconcile the persisted host id with live Browser Host presence."""
    if not intervention.requires_browser_handoff:
        return HostPresence.NOT_REQUIRED
    host_id = intervention.browser_host_id or attempt.browser_host_id
    if not host_id:
        return HostPresence.SESSION_UNAVAILABLE
    if manager is None or not manager.is_connected(host_id):
        return HostPresence.OFFLINE
    live = manager.connected(host_id)
    session_id = intervention.browser_session_id or attempt.browser_session_id
    if live is not None and session_id and session_id not in live.open_sessions:
        return HostPresence.SESSION_UNAVAILABLE
    return HostPresence.CONNECTED


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
        )
        attempt.task_space_id = f"applyuminati:{attempt.id}"
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
            intervention = attempt.pending_intervention
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

    async def persist(self, attempt: ApplicationAttempt) -> None:
        """Commit the attempt so a crash cannot lose a pre-submit marker."""
        await self._repos.attempts.save(attempt)
        await self._repos.session.commit()

    async def resolve(
        self,
        attempt_id: str,
        intervention_id: str,
        resolution: InterventionResolution,
        *,
        payload: dict[str, Any] | None = None,
        manager: BrowserHostManager | None = None,
    ) -> ApplicationAttempt:
        attempt = await self.get(attempt_id)
        intervention = next(
            (item for item in attempt.interventions if item.id == intervention_id), None
        )
        if intervention is None:
            raise NotFoundError(
                f"intervention {intervention_id} not found", code="resource_gone.intervention"
            )
        if not intervention.open and resolution is not InterventionResolution.KEEP_CONTROL:
            return attempt
        if resolution is InterventionResolution.KEEP_CONTROL:
            # Leave the intervention open. Resolving it would drop the item
            # from the inbox while the user still has the browser.
            attempt.record_event(
                AttemptEventKind.CONTROL_KEPT,
                "user is keeping browser control",
                resolution=resolution.value,
            )
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

        if not await self._reclaim_before_resume(attempt, intervention, manager):
            await self._repos.attempts.save(attempt)
            return attempt
        intervention.resolve(resolution, payload=payload)
        attempt.workflow_state = WorkflowState.PENDING
        attempt.record_event(
            AttemptEventKind.RESUMED,
            "user returned control",
            resolution=resolution.value,
        )
        await self._repos.attempts.save(attempt)
        await self.enqueue_resume(attempt)
        return attempt

    async def _reclaim_before_resume(
        self,
        attempt: ApplicationAttempt,
        intervention: HumanIntervention,
        manager: BrowserHostManager | None,
    ) -> bool:
        """Take the browser back before a worker may act. Failure stays paused."""
        if not intervention.requires_browser_handoff:
            return True
        unavailable = (
            manager is None
            or not attempt.browser_host_id
            or not manager.is_connected(attempt.browser_host_id)
        )
        if unavailable:
            attempt.record_event(
                AttemptEventKind.INTERVENTION_RECLAIM_FAILED,
                "Browser Host unavailable; intervention remains open",
            )
            return False
        session = await self.bind_session(attempt, manager=manager)
        detail: str | None = None
        if session is None:
            detail = "could not attach the host session to reclaim control"
        else:
            result = await session.reclaim_control(confirmed_by_user=True)
            if not result.ok:
                detail = result.detail or "reclaim_control failed; user still has the browser"
            else:
                owner = await session.control_state()
                if owner is not ControlOwner.AGENT:
                    detail = "browser is still owned by the user after reclaim"
        if detail is not None:
            attempt.record_event(AttemptEventKind.INTERVENTION_RECLAIM_FAILED, detail)
            return False
        return True

    async def enqueue_resume(self, attempt: ApplicationAttempt) -> None:
        """Queue exactly one resumable attempt task. Idempotent while pending."""
        queue = TaskQueue(self._repos.tasks)
        await queue.submit(
            APPLICATION_ATTEMPT_KIND,
            {"attempt_id": attempt.id},
            idempotency_key=f"{APPLICATION_ATTEMPT_KIND}:{attempt.id}",
        )

    async def bind_session(
        self,
        attempt: ApplicationAttempt,
        *,
        manager: BrowserHostManager | None = None,
        session_factory: SessionFactory | None = None,
    ) -> BrowserSession | None:
        """Attach the attempt to a live host session, or a test factory."""
        if session_factory is not None:
            return await session_factory(attempt)
        if manager is None or not attempt.browser_host_id:
            return None
        if not manager.is_connected(attempt.browser_host_id):
            return None
        session_id = attempt.browser_session_id
        if not session_id:
            result = await manager.dispatch(
                attempt.browser_host_id,
                HostCommand.CREATE_SESSION,
                params={"backend": attempt.browser_backend} if attempt.browser_backend else {},
                idempotency_key=f"create-session:{attempt.id}",
            )
            if not result.ok or not result.result.get("session_id"):
                return None
            session_id = str(result.result["session_id"])
            attempt.browser_session_id = session_id
        return HostedBrowserSession(manager, attempt.browser_host_id, session_id)

    async def activate_browser(
        self,
        attempt: ApplicationAttempt,
        *,
        manager: BrowserHostManager,
        instruction: str,
    ) -> dict[str, Any]:
        """Ask the live host to surface the session for a human."""
        open_item = attempt.pending_intervention
        reference = open_item or (attempt.interventions[-1] if attempt.interventions else None)
        if reference is None:
            return {
                "ok": False,
                "host_presence": HostPresence.SESSION_UNAVAILABLE.value,
                "task_space_id": attempt.task_space_id,
                "detail": "No open intervention is attached to this attempt.",
            }
        presence = host_presence(attempt, reference, manager)
        if presence is HostPresence.OFFLINE:
            return {
                "ok": False,
                "host_presence": presence.value,
                "task_space_id": attempt.task_space_id,
                "detail": "The Mac Browser Host is offline. Start applyuminati-browser-host.",
            }
        if presence is HostPresence.SESSION_UNAVAILABLE:
            return {
                "ok": False,
                "host_presence": presence.value,
                "task_space_id": attempt.task_space_id,
                "detail": "The browser session is not available on the host.",
            }
        session = await self.bind_session(attempt, manager=manager)
        if session is None:
            return {
                "ok": False,
                "host_presence": HostPresence.SESSION_UNAVAILABLE.value,
                "task_space_id": attempt.task_space_id,
                "detail": "Could not attach the host session.",
            }
        result = await session.request_human_control(instruction)
        return {
            "ok": result.ok,
            "host_presence": HostPresence.CONNECTED.value,
            "task_space_id": attempt.task_space_id,
            "detail": result.detail or "The host was asked to open the browser task space.",
        }

    async def run_step(
        self,
        attempt: ApplicationAttempt,
        session: BrowserSession,
        context: DriverContext,
        *,
        driver: Any | None = None,
    ) -> ApplicationAttempt:
        selected = driver
        if selected is None:
            matched = detect_driver(context.job.apply_url or context.job.canonical_url)
            if matched is None:
                attempt.workflow_state = WorkflowState.FAILED
                await self._repos.attempts.save(attempt)
                return attempt
            selected, _detection = matched
        if context.persist is None:
            context.persist = self.persist
        outcome = await selected.run(attempt, session, context)
        if outcome.kind is DriverOutcomeKind.WAITING_FOR_HUMAN:
            attempt.workflow_state = WorkflowState.WAITING_FOR_HUMAN
        await self._repos.attempts.save(attempt)
        return attempt
