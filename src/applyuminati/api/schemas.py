"""API data-transfer objects.

This module is the contract between the FastAPI layer and the React client.
It is deliberately separate from the domain models: the wire format may lag or
flatten the domain, and domain refactors must not silently break the UI.

Two invariants:

* No secret ever appears here. Provider configuration is exposed as
  ``has_api_key: bool``, never as the key.
* Every response that reports a judgement (a score, a recommendation, a
  verification state) also carries the evidence or the uncertainty behind it,
  so the UI can always answer "why".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.models.application import ApplicationState
from applyuminati.core.models.common import EmploymentType, RemoteMode, SeniorityLevel
from applyuminati.core.models.job import AtsVendor, SourceTier, VerificationState
from applyuminati.core.models.scoring import Recommendation, ScoreDimension
from applyuminati.core.registry import HealthState
from applyuminati.core.strategy import SearchStrategy

ItemT = TypeVar("ItemT")

_CFG = ConfigDict(extra="forbid")


class Page(BaseModel, Generic[ItemT]):
    """Offset-paginated envelope."""

    model_config = _CFG

    items: list[ItemT]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class ComponentHealth(BaseModel):
    model_config = _CFG

    name: str
    kind: str
    state: HealthState
    detail: str = ""
    facts: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    model_config = _CFG

    status: str
    version: str
    database_ok: bool
    schema_version: str | None = None
    execution_mode: str
    profile_configured: bool
    enabled_sources: list[str] = Field(default_factory=list)
    checked_at: datetime


class BackendHealthResponse(BaseModel):
    """Availability of every registered backend, grouped by extension point."""

    model_config = _CFG

    sources: list[ComponentHealth] = Field(default_factory=list)
    llm: list[ComponentHealth] = Field(default_factory=list)
    browsers: list[ComponentHealth] = Field(default_factory=list)
    agents: list[ComponentHealth] = Field(default_factory=list)
    email: list[ComponentHealth] = Field(default_factory=list)
    #: Plugins that failed to import, surfaced rather than hidden.
    load_errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class ClaimSummary(BaseModel):
    model_config = _CFG

    id: str
    statement: str
    level: str
    tags: list[str] = Field(default_factory=list)
    provenance_kinds: list[str] = Field(default_factory=list)


class ProfileResponse(BaseModel):
    model_config = _CFG

    id: str
    label: str
    #: Raw JSON Resume document, exactly as it round-trips.
    resume: dict[str, Any]
    name: str | None = None
    headline: str | None = None
    email: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    targets: dict[str, Any] = Field(default_factory=dict)
    strategy: SearchStrategy
    claim_levels: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ProfileImportRequest(BaseModel):
    model_config = _CFG

    #: A JSON Resume document.
    resume: dict[str, Any]
    label: str = "default"
    #: Replace the existing profile rather than failing when one exists.
    replace: bool = False


class ProfileImportResponse(BaseModel):
    model_config = _CFG

    profile: ProfileResponse
    claims_created: int
    metrics_extracted: int
    #: Fields the importer could not interpret. Reported, never dropped silently.
    warnings: list[str] = Field(default_factory=list)


class PreferencesUpdateRequest(BaseModel):
    model_config = _CFG

    titles: list[str] | None = None
    locations: list[str] | None = None
    remote_modes: list[RemoteMode] | None = None
    employment_types: list[EmploymentType] | None = None
    seniority: SeniorityLevel | None = None
    minimum_compensation: float | None = None
    compensation_currency: str | None = None
    strategy: SearchStrategy | None = None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class SourceInfo(BaseModel):
    model_config = _CFG

    slug: str
    name: str
    description: str = ""
    tier: SourceTier
    ats: AtsVendor
    enabled: bool
    capabilities: list[str] = Field(default_factory=list)
    requires_auth: bool = False
    blocking: str = "none"
    health: ComponentHealth | None = None
    #: Plugin-specific options currently configured, secrets removed.
    options: dict[str, Any] = Field(default_factory=dict)
    #: JSON Schema for this plugin's options, so the UI can render a form.
    options_schema: dict[str, Any] | None = None
    last_run_at: datetime | None = None
    last_run_jobs: int = 0
    consecutive_failures: int = 0


class SourceToggleRequest(BaseModel):
    model_config = _CFG

    options: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobSourceInfo(BaseModel):
    model_config = _CFG

    source: str
    tier: SourceTier
    url: str
    source_job_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    confidence: float


class ScoreDimensionInfo(BaseModel):
    model_config = _CFG

    dimension: ScoreDimension
    score: float
    weight: float
    confidence: float
    rationale: str = ""
    llm_adjusted: bool = False


class FitScoreInfo(BaseModel):
    model_config = _CFG

    id: str
    overall: float
    confidence: float
    recommendation: Recommendation
    explanation: str = ""
    baseline_overall: float | None = None
    scorer_version: str
    llm_provider: str | None = None
    llm_model: str | None = None
    dimensions: list[ScoreDimensionInfo] = Field(default_factory=list)
    matched_evidence: list[dict[str, Any]] = Field(default_factory=list)
    missing_requirements: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    scored_at: datetime


class JobSummary(BaseModel):
    """Row shape for the jobs table."""

    model_config = _CFG

    id: str
    title: str
    company: str
    location: str
    remote_mode: RemoteMode
    employment_type: EmploymentType
    seniority: SeniorityLevel
    ats: AtsVendor
    sources: list[str] = Field(default_factory=list)
    canonical_url: str
    apply_url: str | None = None
    compensation: str | None = None
    posted_at: datetime | None = None
    discovered_at: datetime
    #: Days since any source last saw the posting.
    freshness_days: float
    verification: VerificationState
    fit_score: float | None = None
    recommendation: Recommendation | None = None
    application_state: ApplicationState | None = None
    duplicate_source_count: int = 0


class JobDetail(JobSummary):
    """Everything the job detail page needs, in one request."""

    model_config = _CFG

    description: str | None = None
    requirements: list[str] = Field(default_factory=list)
    preferred_qualifications: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    locations: list[dict[str, Any]] = Field(default_factory=list)
    source_records: list[JobSourceInfo] = Field(default_factory=list)
    score: FitScoreInfo | None = None
    merged_job_ids: list[str] = Field(default_factory=list)
    #: Transitions currently legal for the linked application.
    available_actions: list[str] = Field(default_factory=list)


class DiscoverRequest(BaseModel):
    model_config = _CFG

    #: Restrict to these source slugs. Empty means "every enabled source".
    sources: list[str] = Field(default_factory=list)
    #: Override the profile's target titles for this run.
    queries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    #: Run synchronously and return the finished run. Used by tests and the CLI.
    wait: bool = False


class ScoreRequest(BaseModel):
    model_config = _CFG

    job_ids: list[str] = Field(default_factory=list)
    #: Re-score jobs that already have a score.
    rescore: bool = False
    #: Run the optional LLM enrichment pass on top of the deterministic score.
    use_llm: bool = False
    limit: int = 100
    wait: bool = False


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


class ApplicationEventInfo(BaseModel):
    model_config = _CFG

    id: str
    occurred_at: datetime
    from_state: ApplicationState | None = None
    to_state: ApplicationState | None = None
    actor: str
    actor_detail: str | None = None
    reason: str = ""
    message: str | None = None
    failure_category: str | None = None


class ApplicationSummary(BaseModel):
    model_config = _CFG

    id: str
    job_id: str
    job_title: str
    company: str
    state: ApplicationState
    fit_score: float | None = None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None
    needs_attention: bool = False


class ApplicationDetail(ApplicationSummary):
    model_config = _CFG

    external_reference: str | None = None
    notes: str | None = None
    events: list[ApplicationEventInfo] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    allowed_transitions: list[ApplicationState] = Field(default_factory=list)


class TransitionRequest(BaseModel):
    model_config = _CFG

    to_state: ApplicationState
    reason: str = "user.manual"
    message: str | None = None


# ---------------------------------------------------------------------------
# Runs and dashboard
# ---------------------------------------------------------------------------


class RunSummary(BaseModel):
    model_config = _CFG

    id: str
    kind: str
    state: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    stats: dict[str, int] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    triggered_by: str = "api"


class ActivityItem(BaseModel):
    model_config = _CFG

    at: datetime
    kind: str
    summary: str
    job_id: str | None = None
    application_id: str | None = None


class DashboardResponse(BaseModel):
    model_config = _CFG

    total_jobs: int
    shortlisted: int
    ready: int
    submitted: int
    needs_attention: int
    scored: int
    unscored: int
    by_recommendation: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, int] = Field(default_factory=dict)
    by_application_state: dict[str, int] = Field(default_factory=dict)
    recent_activity: list[ActivityItem] = Field(default_factory=list)
    latest_run: RunSummary | None = None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class ProviderInfo(BaseModel):
    model_config = _CFG

    name: str
    kind: str
    enabled: bool
    base_url: str | None = None
    default_model: str | None = None
    fast_model: str | None = None
    #: Never the key itself.
    has_api_key: bool = False
    health: ComponentHealth | None = None


class SettingsResponse(BaseModel):
    model_config = _CFG

    execution_mode: str
    data_dir: str
    database: str
    log_level: str
    llm_enabled: bool
    default_provider: str | None = None
    providers: list[ProviderInfo] = Field(default_factory=list)
    browser_preferred: list[str] = Field(default_factory=list)
    agents_enabled: bool = False
    agents_preferred: list[str] = Field(default_factory=list)
    email_accounts: list[str] = Field(default_factory=list)
    strategy: SearchStrategy


class StrategyUpdateRequest(BaseModel):
    model_config = _CFG

    strategy: SearchStrategy | None = None
    #: Materialise a named preset into concrete values.
    preset: str | None = None


class ErrorResponse(BaseModel):
    """Uniform error envelope. Mirrors ``ApplyuminatiError.to_dict``."""

    model_config = _CFG

    code: str
    category: str
    message: str
    recovery: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ActivityItem",
    "ApplicationDetail",
    "ApplicationEventInfo",
    "ApplicationSummary",
    "BackendHealthResponse",
    "ClaimSummary",
    "ComponentHealth",
    "DashboardResponse",
    "DiscoverRequest",
    "ErrorResponse",
    "FitScoreInfo",
    "HealthResponse",
    "JobDetail",
    "JobSourceInfo",
    "JobSummary",
    "Page",
    "PreferencesUpdateRequest",
    "ProfileImportRequest",
    "ProfileImportResponse",
    "ProfileResponse",
    "ProviderInfo",
    "RunSummary",
    "ScoreDimensionInfo",
    "ScoreRequest",
    "SettingsResponse",
    "SourceInfo",
    "SourceToggleRequest",
    "StrategyUpdateRequest",
    "TransitionRequest",
]
