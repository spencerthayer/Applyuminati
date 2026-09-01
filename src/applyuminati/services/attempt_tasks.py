"""SQLite task wiring for application attempts.

Handlers live here because only the services layer may depend on attempts,
drivers, and the Browser Host together. The queue itself stays generic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.applications.driver import ApplicationDriver, DriverContext
from applyuminati.browser.base import BrowserSession
from applyuminati.core.errors import NotFoundError
from applyuminati.core.logging import get_logger
from applyuminati.core.models.execution import ApplicationAttempt, InterventionReason, WorkflowState
from applyuminati.core.models.profile import CareerProfile
from applyuminati.services.attempt_service import APPLICATION_ATTEMPT_KIND, AttemptService
from applyuminati.services.container import Repositories, get_container
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
        attempt.open_intervention(
            InterventionReason.USER_REVIEW,
            (
                "The Browser Host is not connected or the session is gone. "
                "Connect the Mac host, then choose Done, continue."
            ),
            requires_browser_handoff=True,
        )
        await repos.attempts.save(attempt)
        return {"status": WorkflowState.WAITING_FOR_HUMAN.value, "attempt_id": attempt.id}
    driver_context = DriverContext(job=job, profile=profile, mode=attempt.submission_mode)
    updated = await service.run_step(attempt, session, driver_context, driver=driver)
    context.logger.info(
        "attempt.step_finished",
        attempt_id=updated.id,
        workflow_state=updated.workflow_state.value,
    )
    return {"status": updated.workflow_state.value, "attempt_id": updated.id}


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
