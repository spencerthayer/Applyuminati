"""Idempotency: never submit the same application twice.

The fingerprint is over ``(profile_id, company_key, title_key, primary location)``
— deliberately NOT over the URL, because the same role reached through an
aggregator and through the employer's ATS must fingerprint identically. This
is the guard that makes autonomous submission safe to attempt.
"""

from __future__ import annotations

from applyuminati.core.ids import stable_id
from applyuminati.core.models.application import Application
from applyuminati.core.models.job import Job
from applyuminati.db.repositories.applications import ApplicationRepository

__all__ = ["already_applied", "submission_fingerprint"]


def submission_fingerprint(profile_id: str, job: Job) -> str:
    """A stable hash identifying one role for one user."""
    location = job.locations[0].display().lower() if job.locations else ""
    return stable_id("application", profile_id, job.company_key, job.title_key, location)


async def already_applied(
    repo: ApplicationRepository, profile_id: str, job: Job
) -> Application | None:
    """Return an existing submitted application for the same role, if any."""
    existing = await repo.get_for_job(job.id, profile_id)
    if existing is not None and existing.already_submitted:
        return existing
    # Also check by fingerprint in case the job row was re-created.
    fp = submission_fingerprint(profile_id, job)
    applications, _ = await repo.list(limit=200)
    for app in applications:
        if app.submission_fingerprint == fp and app.already_submitted:
            return app
    return None
