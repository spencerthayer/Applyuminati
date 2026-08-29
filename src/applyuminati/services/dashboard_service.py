"""Dashboard aggregation.

Counts the user asked for, plus a recent-activity feed assembled from the
application event log. Everything here is a read; nothing is cached, because
at local single-user scale the queries are trivial and a stale dashboard is
worse than a slightly slower one.
"""

from __future__ import annotations

from applyuminati.core.models.application import (
    ATTENTION_STATES,
    SUBMITTED_STATES,
    ApplicationState,
)
from applyuminati.services.container import Repositories
from applyuminati.services.views import ActivityEntry, DashboardView

_ACTIVITY_LIMIT = 20


class DashboardService:
    def __init__(self, repos: Repositories) -> None:
        self._repos = repos

    async def build(self) -> DashboardView:
        profile = await self._repos.profiles.get_active()
        profile_id = profile.id if profile else None

        _, total_jobs = await self._repos.jobs.list(limit=1)
        by_state = await self._repos.applications.counts_by_state(profile_id)
        by_source = await self._repos.jobs.count_by_source()
        by_recommendation = (
            await self._repos.scores.counts_by_recommendation(profile_id) if profile_id else {}
        )

        scored = sum(by_recommendation.values())
        submitted = sum(by_state.get(state.value, 0) for state in SUBMITTED_STATES)
        needs_attention = sum(by_state.get(state.value, 0) for state in ATTENTION_STATES)

        runs = await self._repos.runs.list(limit=1)

        return DashboardView(
            total_jobs=total_jobs,
            shortlisted=by_state.get(ApplicationState.SHORTLISTED.value, 0),
            ready=by_state.get(ApplicationState.READY.value, 0),
            submitted=submitted,
            needs_attention=needs_attention,
            scored=scored,
            unscored=max(0, total_jobs - scored),
            by_recommendation=by_recommendation,
            by_source=by_source,
            by_application_state=by_state,
            recent_activity=await self._recent_activity(),
            latest_run=runs[0] if runs else None,
        )

    async def _recent_activity(self) -> list[ActivityEntry]:
        """Newest application events, rendered as one line each."""
        applications, _ = await self._repos.applications.list(limit=_ACTIVITY_LIMIT)
        entries: list[ActivityEntry] = []
        for application in applications:
            for event in application.events[-3:]:
                if not event.is_transition:
                    continue
                target = event.to_state.value if event.to_state else "?"
                entries.append(
                    ActivityEntry(
                        at=event.occurred_at,
                        kind="application",
                        summary=f"{target} ({event.reason or 'no reason recorded'})",
                        job_id=application.job_id,
                        application_id=application.id,
                    )
                )
        entries.sort(key=lambda entry: entry.at, reverse=True)
        return entries[:_ACTIVITY_LIMIT]


__all__ = ["DashboardService"]
