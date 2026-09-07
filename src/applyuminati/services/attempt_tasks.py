"""SQLite task wiring for application attempts.

Handlers live here because only the services layer may depend on attempts,
drivers, and the Browser Host together. The queue itself stays generic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.applications.driver import ApplicationDriver, DriverContext, detect_driver
from applyuminati.browser.base import BrowserCapability, BrowserSession
from applyuminati.browser.selection import select_browser
from applyuminati.core.errors import BackendUnavailableError, NotFoundError
from applyuminati.core.logging import get_logger
from applyuminati.core.models.execution import (
    ApplicationAttempt,
    AttemptEventKind,
    InterventionReason,
    WorkflowState,
)
from applyuminati.core.models.job import Job
from applyuminati.core.models.profile import CareerProfile
from applyuminati.services.attempt_service import APPLICATION_ATTEMPT_KIND, AttemptService
from applyuminati.services.container import Repositories, get_container
from applyuminati.services.local_browser import LocalBrowserManager
from applyuminati.tasks.handlers import HANDLER_REGISTRY, TaskContext, register_handler
from applyuminati.tasks.queue import TaskQueue
from applyuminati.tasks.worker import TaskWorker

log = get_logger(__name__)

__all__ = [
    "APPLICATION_ATTEMPT_KIND",
    "ApplicationAttemptPayload",
    "register_attempt_handlers",
    "run_application_attempt",
    "run_attempt_worker_forever",
]


class ApplicationAttemptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1)


SessionFactory = Callable[[ApplicationAttempt], Awaitable[BrowserSession | None]]


async def run_application_attempt(
    payload: ApplicationAttemptPayload,
    context: TaskContext,
    *,
    repos: Repositories | None = None,
    driver: ApplicationDriver | None = None,
    session_factory: SessionFactory | None = None,
) -> dict[str, Any]:
    """Reload one attempt and advance it one driver step."""
    owns_session = repos is None
    container = get_container()
    if owns_session:
        async with container.repositories() as opened:
            return await _run(payload, context, opened, driver, session_factory)
    assert repos is not None
    return await _run(payload, context, repos, driver, session_factory)


async def _run(
    payload: ApplicationAttemptPayload,
    context: TaskContext,
    repos: Repositories,
    driver: ApplicationDriver | None,
    session_factory: SessionFactory | None,
) -> dict[str, Any]:
    service = AttemptService(repos)
    try:
        attempt = await service.get(payload.attempt_id)
    except NotFoundError:
        return {"status": "missing", "attempt_id": payload.attempt_id}
    job = await repos.jobs.get(attempt.job_id)
    if job is None:
        attempt.workflow_state = WorkflowState.FAILED
        await repos.attempts.save(attempt)
        return {"status": attempt.workflow_state.value, "attempt_id": attempt.id}
    profile = (
        await repos.profiles.get(attempt.profile_id)
        if attempt.profile_id
        else await repos.profiles.get_active()
    )
    if profile is None:
        profile = CareerProfile()
    manager = get_container().browser_hosts
    session = await service.bind_session(attempt, manager=manager, session_factory=session_factory)
    if session is None:
        selected = await _select_or_pause(attempt, job, driver, repos)
        if selected.get("selected_backend") is None:
            return selected
        try:
            session = await _local_manager().acquire(attempt)
        except BackendUnavailableError as exc:
            # Belt and braces: selection already checked the contract, so this
            # refuses rather than substitutes or crashes the task loop.
            snapshot = attempt.browser_requirements or {}
            needs_handoff = BrowserCapability.HUMAN_HANDOFF.value in snapshot.get("required", [])
            attempt.open_intervention(
                InterventionReason.USER_REVIEW,
                f"The selected browser cannot run this application: {exc}",
                requires_browser_handoff=needs_handoff,
            )
            attempt.workflow_state = WorkflowState.WAITING_FOR_HUMAN
            await repos.attempts.save(attempt)
            return {"status": attempt.workflow_state.value, "attempt_id": attempt.id}
        attempt.browser_session_id = attempt.id
        await repos.attempts.save(attempt)
    driver_context = DriverContext(job=job, profile=profile, mode=attempt.submission_mode)
    updated = await service.run_step(attempt, session, driver_context, driver=driver)
    context.logger.info(
        "attempt.step_finished",
        attempt_id=updated.id,
        workflow_state=updated.workflow_state.value,
    )
    result = {"status": updated.workflow_state.value, "attempt_id": updated.id}
    if attempt.browser_backend:
        result["selected_backend"] = attempt.browser_backend
    return result


def _local_manager() -> LocalBrowserManager:
    """The process-owned local browser manager for the application worker."""
    container = get_container()
    manager = getattr(container, "local_browsers", None)
    if manager is None:
        manager = LocalBrowserManager(container.settings)
        container.local_browsers = manager  # type: ignore[attr-defined]
    return manager


async def _select_or_pause(
    attempt: ApplicationAttempt,
    job: Job,
    driver: ApplicationDriver | None,
    repos: Repositories,
) -> dict[str, Any]:
    """Decide execution for an attempt no session could be bound to.

    A bound attempt never reaches this: durable resume identity wins over
    selection, and ``bind_session`` already re-enters the exact host session.
    For an unbound attempt the apply URL's driver states its browser contract
    and ``select_browser`` turns it into a decision. PR #9 persists that
    decision and stops; PR #10 supplies the local session acquisition that
    acts on it.
    """
    if driver is None:
        matched = detect_driver(job.apply_url or job.canonical_url)
        if matched is None:
            attempt.open_intervention(
                InterventionReason.USER_REVIEW,
                (
                    "No application driver matches the apply URL for this job, "
                    "so the attempt cannot be driven. Review the job or apply "
                    "manually."
                ),
                requires_browser_handoff=False,
            )
            attempt.workflow_state = WorkflowState.WAITING_FOR_HUMAN
            await repos.attempts.save(attempt)
            return {"status": attempt.workflow_state.value, "attempt_id": attempt.id}
        driver = matched[0]
    requirements = driver.metadata.requirements
    needs_handoff = BrowserCapability.HUMAN_HANDOFF in requirements.required
    try:
        backend, _health = await select_browser(get_container().settings, requirements)
    except BackendUnavailableError as exc:
        if needs_handoff:
            instruction = (
                "This application requires browser handoff, but no available "
                f"browser satisfies its contract. {exc} "
                "Connect a compatible Browser Host, then choose Done, continue."
            )
        else:
            instruction = f"The application cannot start: {exc}"
        attempt.open_intervention(
            InterventionReason.USER_REVIEW,
            instruction,
            requires_browser_handoff=needs_handoff,
        )
        attempt.workflow_state = WorkflowState.WAITING_FOR_HUMAN
        await repos.attempts.save(attempt)
        return {"status": attempt.workflow_state.value, "attempt_id": attempt.id}
    snapshot = {
        "required": sorted(cap.value for cap in requirements.required),
        "preferred": sorted(cap.value for cap in requirements.preferred),
    }
    attempt.browser_backend = backend.metadata.slug
    attempt.browser_requirements = snapshot
    attempt.record_event(
        AttemptEventKind.BROWSER_SELECTED,
        "capability selection chose the backend for this attempt",
        backend=attempt.browser_backend,
        requirements=snapshot,
    )
    await repos.attempts.save(attempt)
    log.info(
        "attempt.browser_selected",
        attempt_id=attempt.id,
        backend=attempt.browser_backend,
        requirements=requirements.describe(),
    )
    return {
        "status": attempt.workflow_state.value,
        "attempt_id": attempt.id,
        "selected_backend": attempt.browser_backend,
    }


def register_attempt_handlers() -> None:
    """Idempotent. Safe to call from every ServiceContainer construction."""
    if APPLICATION_ATTEMPT_KIND in HANDLER_REGISTRY:
        return

    @register_handler(APPLICATION_ATTEMPT_KIND, ApplicationAttemptPayload)
    async def _handle(payload: ApplicationAttemptPayload, context: TaskContext) -> dict[str, Any]:
        return await run_application_attempt(payload, context)


async def run_attempt_worker_forever(
    *,
    poll_interval: float = 1.0,
    stop_event: Any | None = None,
) -> None:
    """Claim application-attempt tasks using a fresh unit of work each poll."""
    stop = stop_event or asyncio.Event()
    container = get_container()
    while not stop.is_set():
        did_work = False
        try:
            async with container.repositories() as repos:
                worker = TaskWorker(TaskQueue(repos.tasks))
                did_work = await worker.run_once(kinds=[APPLICATION_ATTEMPT_KIND])
        except asyncio.CancelledError:
            raise
        except Exception:
            # A handler or the database raising here would otherwise end the
            # coroutine and leave PENDING attempts unclaimed for the rest of
            # the process lifetime. Log it and wait one interval instead.
            log.exception("attempt_worker.poll_failed")
            did_work = False
        if not did_work:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except TimeoutError:
                continue
