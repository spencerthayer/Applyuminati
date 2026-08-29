"""Application lifecycle operations.

Every state change goes through :class:`ApplicationMachine`, so an illegal
transition is refused with the legal targets named rather than silently
written. The event log is appended before the cached ``state`` column is
saved, which means a crash between the two leaves the log authoritative and
:meth:`ApplicationMachine.replay` can repair the cache.
"""

from __future__ import annotations

from collections.abc import Sequence

from applyuminati.applications.idempotency import already_applied
from applyuminati.applications.machine import ApplicationMachine
from applyuminati.core.errors import NotFoundError
from applyuminati.core.logging import get_logger
from applyuminati.core.models.application import (
    ActorKind,
    Application,
    ApplicationState,
)
from applyuminati.services.container import Repositories
from applyuminati.services.views import ApplicationPage, ApplicationView

log = get_logger(__name__)


class ApplicationService:
    def __init__(self, repos: Repositories) -> None:
        self._repos = repos
        self._machine = ApplicationMachine()

    async def list(
        self,
        *,
        states: Sequence[ApplicationState] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ApplicationPage:
        applications, total = await self._repos.applications.list(
            states=[s.value for s in states] if states else None,
            limit=limit,
            offset=offset,
        )
        views = [view for view in [await self._decorate(app) for app in applications] if view]
        return ApplicationPage(items=views, total=total, limit=limit, offset=offset)

    async def get(self, application_id: str) -> ApplicationView:
        application = await self._repos.applications.get(application_id)
        if application is None:
            raise NotFoundError(
                f"application {application_id} not found", code="resource_gone.application"
            )
        view = await self._decorate(application)
        if view is None:
            raise NotFoundError(
                f"application {application_id} references a missing job",
                code="resource_gone.job",
            )
        return view

    async def transition(
        self,
        application_id: str,
        to_state: ApplicationState,
        *,
        reason: str = "user.manual",
        message: str | None = None,
        actor: ActorKind = ActorKind.USER,
        actor_detail: str | None = None,
    ) -> ApplicationView:
        """Move an application. Raises when the transition is not legal."""
        application = await self._repos.applications.get(application_id)
        if application is None:
            raise NotFoundError(
                f"application {application_id} not found", code="resource_gone.application"
            )

        event = self._machine.transition(
            application,
            to_state,
            actor=actor,
            actor_detail=actor_detail,
            reason=reason,
            message=message,
        )
        await self._repos.applications.append_event(event)
        await self._repos.applications.save(application)
        log.info(
            "application.transitioned",
            application_id=application.id,
            from_state=event.from_state.value if event.from_state else None,
            to_state=to_state.value,
            reason=reason,
        )
        return await self.get(application_id)

    async def guard_duplicate(self, job_id: str, profile_id: str) -> Application | None:
        """Return an existing submitted application for the same role, if any.

        Called before any submission path. Matching is by role fingerprint, not
        by URL, so applying through an aggregator after applying through the
        employer's ATS is still caught.
        """
        job = await self._repos.jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"job {job_id} not found", code="resource_gone.job")
        return await already_applied(self._repos.applications, profile_id, job)

    async def counts_by_state(self, profile_id: str | None = None) -> dict[str, int]:
        return await self._repos.applications.counts_by_state(profile_id)

    async def _decorate(self, application: Application) -> ApplicationView | None:
        job = await self._repos.jobs.get(application.job_id)
        if job is None:
            return None
        score = await self._repos.scores.latest_for(application.job_id, application.profile_id)
        return ApplicationView(application=application, job=job, score=score)


__all__ = ["ApplicationService"]
