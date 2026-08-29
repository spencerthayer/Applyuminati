"""Job read models.

Listing a job for the UI needs three things joined: the posting, its newest
fit score, and the state of the user's application. Doing that join here — in
one place, with one batched score lookup — keeps the API handlers thin and
avoids the N+1 query that this shape invites.
"""

from __future__ import annotations

from collections.abc import Sequence

from applyuminati.core.errors import NotFoundError
from applyuminati.core.models.application import ApplicationState, allowed_transitions
from applyuminati.core.models.common import RemoteMode
from applyuminati.core.models.job import Job, VerificationState
from applyuminati.core.models.scoring import Recommendation
from applyuminati.services.container import Repositories
from applyuminati.services.views import JobPage, JobView


class JobService:
    def __init__(self, repos: Repositories) -> None:
        self._repos = repos

    async def list(
        self,
        *,
        query: str | None = None,
        sources: Sequence[str] | None = None,
        recommendation: Recommendation | None = None,
        min_score: float | None = None,
        states: Sequence[ApplicationState] | None = None,
        companies: Sequence[str] | None = None,
        remote_modes: Sequence[RemoteMode] | None = None,
        verification: VerificationState | None = None,
        has_score: bool | None = None,
        sort: str = "discovered_at",
        descending: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> JobPage:
        profile = await self._repos.profiles.get_active()
        profile_id = profile.id if profile else ""

        jobs, total = await self._repos.jobs.list(
            query=query,
            sources=list(sources) if sources else None,
            recommendation=recommendation.value if recommendation else None,
            min_score=min_score,
            states=[s.value for s in states] if states else None,
            remote_modes=[rm.value for rm in remote_modes] if remote_modes else None,
            has_score=has_score,
            sort=sort,
            descending=descending,
            limit=limit,
            offset=offset,
        )
        views = await self._decorate(jobs, profile_id)
        return JobPage(items=views, total=total, limit=limit, offset=offset)

    async def get(self, job_id: str) -> JobView:
        job = await self._repos.jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"job {job_id} not found", code="resource_gone.job")
        profile = await self._repos.profiles.get_active()
        profile_id = profile.id if profile else ""
        views = await self._decorate([job], profile_id)
        return views[0]

    async def available_actions(self, view: JobView) -> list[str]:
        """Transitions the UI may offer for this job's application."""
        if view.application_state is None:
            return []
        return [state.value for state in allowed_transitions(view.application_state)]

    async def _decorate(self, jobs: list[Job], profile_id: str) -> list[JobView]:
        if not jobs or not profile_id:
            return [JobView(job=job) for job in jobs]

        job_ids = [job.id for job in jobs]
        scores = await self._repos.scores.latest_map(job_ids, profile_id)

        views: list[JobView] = []
        for job in jobs:
            application = await self._repos.applications.get_for_job(job.id, profile_id)
            views.append(
                JobView(
                    job=job,
                    score=scores.get(job.id),
                    application_state=application.state if application else None,
                    application_id=application.id if application else None,
                )
            )
        return views


__all__ = ["JobService"]
