"""Read models returned by services.

Services return domain objects plus these small composites; the API layer maps
them to wire DTOs. Keeping the DTOs out of the service layer preserves the
dependency direction (``api`` may import ``services``, never the reverse) and
means a wire-format change cannot ripple into application logic.

These are deliberately thin — three or four fields each. Anything larger would
be duplicating the domain model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from applyuminati.core.models.application import Application, ApplicationState
from applyuminati.core.models.job import Job
from applyuminati.core.models.scoring import FitScore
from applyuminati.core.models.task import RunRecord
from applyuminati.core.registry import HealthReport


@dataclass(frozen=True, slots=True)
class JobView:
    """A job joined with its newest score and its application state."""

    job: Job
    score: FitScore | None = None
    application_state: ApplicationState | None = None
    application_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobPage:
    items: list[JobView]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ApplicationView:
    application: Application
    job: Job
    score: FitScore | None = None


@dataclass(frozen=True, slots=True)
class ApplicationPage:
    items: list[ApplicationView]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class SourceView:
    """Registry metadata joined with persisted state and a health probe."""

    slug: str
    name: str
    description: str
    tier: str
    ats: str
    enabled: bool
    capabilities: list[str]
    requires_auth: bool
    blocking: str
    options: dict[str, object]
    options_schema: dict[str, object] | None
    health: HealthReport | None = None
    last_run_at: datetime | None = None
    last_run_jobs: int = 0
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True)
class BackendHealthView:
    sources: list[HealthReport] = field(default_factory=list)
    llm: list[HealthReport] = field(default_factory=list)
    browsers: list[HealthReport] = field(default_factory=list)
    agents: list[HealthReport] = field(default_factory=list)
    email: list[HealthReport] = field(default_factory=list)
    load_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    at: datetime
    kind: str
    summary: str
    job_id: str | None = None
    application_id: str | None = None


@dataclass(frozen=True, slots=True)
class DashboardView:
    total_jobs: int
    shortlisted: int
    ready: int
    submitted: int
    needs_attention: int
    scored: int
    unscored: int
    by_recommendation: dict[str, int]
    by_source: dict[str, int]
    by_application_state: dict[str, int]
    recent_activity: list[ActivityEntry]
    latest_run: RunRecord | None = None


@dataclass(frozen=True, slots=True)
class ProfileView:
    """A profile plus the derived counts the UI shows without re-deriving them."""

    profile_id: str
    label: str
    resume: dict[str, object]
    name: str | None
    headline: str | None
    email: str | None
    counts: dict[str, int]
    claim_levels: dict[str, int]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ImportResult:
    profile: ProfileView
    claims_created: int
    metrics_extracted: int
    warnings: list[str]


__all__ = [
    "ActivityEntry",
    "ApplicationPage",
    "ApplicationView",
    "BackendHealthView",
    "DashboardView",
    "ImportResult",
    "JobPage",
    "JobView",
    "ProfileView",
    "SourceView",
]
