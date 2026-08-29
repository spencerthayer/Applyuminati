"""ORM tables.

Shape rule: **anything filtered, sorted or joined on gets a real column;
everything else is JSON.** That keeps the schema small and stable while the
domain models evolve, and it avoids the migration-per-field churn that makes
early-stage schemas painful.

Nested Pydantic structures (locations, compensation, dimension scores,
provenance) live in JSON columns and are re-validated on load, so the database
never becomes a second, divergent definition of the domain.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.db.base import ULID, Base


def _pk() -> Mapped[str]:
    return mapped_column(ULID, primary_key=True, default=new_ulid)


class ProfileRow(Base):
    """The canonical career profile.

    The full :class:`~applyuminati.core.models.profile.CareerProfile` is stored
    as JSON except for the claim ledger, which is a real table because it is
    queried by tag and level during tailoring and questionnaire answering.
    """

    __tablename__ = "profiles"

    id: Mapped[str] = _pk()
    label: Mapped[str] = mapped_column(String(120), default="default", unique=True)
    #: JSON Resume document, kept separately so export is a straight read.
    resume: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: Everything else on CareerProfile except ``claims`` and ``resume``.
    profile_data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    claims: Mapped[list[ClaimRow]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )


class ClaimRow(Base):
    """One statement with an epistemic level. The evidence backbone."""

    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_profile_level", "profile_id", "level"),
        Index("ix_claims_profile_active", "profile_id", "superseded_by"),
    )

    id: Mapped[str] = _pk()
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    statement: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(32), index=True)
    tags: Mapped[list[str]] = mapped_column(default=list)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    provenance: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    #: Non-null when a newer claim replaced this one. Rows are never deleted.
    superseded_by: Mapped[str | None] = mapped_column(ULID, nullable=True)

    profile: Mapped[ProfileRow] = relationship(back_populates="claims")


class JobRow(Base):
    """A canonical job posting, deduplicated across sources."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("identity_key", name="uq_jobs_identity_key"),
        Index("ix_jobs_company_title", "company_key", "title_key"),
        Index("ix_jobs_stage_verification", "stage", "verification"),
        Index("ix_jobs_discovered_at", "discovered_at"),
    )

    id: Mapped[str] = _pk()
    identity_key: Mapped[str] = mapped_column(String(26), index=True)
    canonical_url: Mapped[str] = mapped_column(Text, index=True)
    apply_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    company: Mapped[str] = mapped_column(String(300))
    company_key: Mapped[str] = mapped_column(String(300), index=True)
    company_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(400))
    title_raw: Mapped[str] = mapped_column(String(400))
    title_key: Mapped[str] = mapped_column(String(400), index=True)
    department: Mapped[str | None] = mapped_column(String(200), nullable=True)
    seniority: Mapped[str] = mapped_column(String(32), default="unknown", index=True)

    remote_mode: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    employment_type: Mapped[str] = mapped_column(String(24), default="unknown")
    #: Denormalised for range queries; the full range lives in ``compensation``.
    comp_min_annual: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_max_annual: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    locations: Mapped[list[Any]] = mapped_column(default=list)
    compensation: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    requirements: Mapped[list[str]] = mapped_column(default=list)
    preferred_qualifications: Mapped[list[str]] = mapped_column(default=list)
    skills: Mapped[list[str]] = mapped_column(default=list)

    posted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    valid_through: Mapped[datetime | None] = mapped_column(nullable=True)
    verification: Mapped[str] = mapped_column(String(16), default="unverified")
    stage: Mapped[str] = mapped_column(String(16), default="discovered")

    ats: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    merged_job_ids: Mapped[list[str]] = mapped_column(default=list)

    sources: Mapped[list[JobSourceRow]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )
    scores: Mapped[list[FitScoreRow]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )


class JobSourceRow(Base):
    """Evidence that one source reported one job. Never merged away."""

    __tablename__ = "job_sources"
    __table_args__ = (
        UniqueConstraint("source", "source_job_id", name="uq_job_sources_source_job"),
        Index("ix_job_sources_canonical_url", "canonical_url"),
    )

    id: Mapped[str] = _pk()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    tier: Mapped[str] = mapped_column(String(20), default="aggregator")
    source_job_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)

    job: Mapped[JobRow] = relationship(back_populates="sources")


class FitScoreRow(Base):
    """A scoring verdict. Historical scores are kept, never overwritten."""

    __tablename__ = "fit_scores"
    __table_args__ = (Index("ix_fit_scores_job_profile", "job_id", "profile_id", "scored_at"),)

    id: Mapped[str] = _pk()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    overall: Mapped[float] = mapped_column(Float, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    recommendation: Mapped[str] = mapped_column(String(16), index=True)
    baseline_overall: Mapped[float | None] = mapped_column(Float, nullable=True)
    scorer_version: Mapped[str] = mapped_column(String(48), default="baseline/1")
    llm_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_prompt_version: Mapped[str | None] = mapped_column(String(48), nullable=True)
    explanation: Mapped[str] = mapped_column(Text, default="")
    dimensions: Mapped[list[Any]] = mapped_column(default=list)
    matched_evidence: Mapped[list[Any]] = mapped_column(default=list)
    missing_requirements: Mapped[list[Any]] = mapped_column(default=list)
    uncertainties: Mapped[list[str]] = mapped_column(default=list)
    scored_at: Mapped[datetime] = mapped_column(default=utcnow)

    job: Mapped[JobRow] = relationship(back_populates="scores")


class ApplicationRow(Base):
    """The user's pursuit of one job."""

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("job_id", "profile_id", name="uq_applications_job_profile"),
        Index("ix_applications_state", "state", "updated_at"),
    )

    id: Mapped[str] = _pk()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(24), default="discovered", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fit_score_id: Mapped[str | None] = mapped_column(ULID, nullable=True)
    #: Idempotency guard so the same role is never submitted twice.
    submission_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    events: Mapped[list[ApplicationEventRow]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ApplicationEventRow.occurred_at",
    )
    artifacts: Mapped[list[ApplicationArtifactRow]] = relationship(
        back_populates="application", cascade="all, delete-orphan", lazy="selectin"
    )


class ApplicationEventRow(Base):
    """Append-only history. The state column is a cache of this table."""

    __tablename__ = "application_events"
    __table_args__ = (Index("ix_application_events_app_time", "application_id", "occurred_at"),)

    id: Mapped[str] = _pk()
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE")
    )
    occurred_at: Mapped[datetime] = mapped_column(default=utcnow)
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    actor: Mapped[str] = mapped_column(String(20), default="system")
    actor_detail: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reason: Mapped[str] = mapped_column(String(120), default="")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    failure_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ULID, nullable=True)
    task_id: Mapped[str | None] = mapped_column(ULID, nullable=True)

    application: Mapped[ApplicationRow] = relationship(back_populates="events")


class ApplicationArtifactRow(Base):
    """A generated file, addressed relative to the data directory."""

    __tablename__ = "application_artifacts"

    id: Mapped[str] = _pk()
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40))
    relative_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bytes_written: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    evidence_claim_ids: Mapped[list[str]] = mapped_column(default=list)

    application: Mapped[ApplicationRow] = relationship(back_populates="artifacts")


class CompanyResearchRow(Base):
    """Cached employer research, with per-finding freshness in the JSON blob."""

    __tablename__ = "company_research"
    __table_args__ = (UniqueConstraint("company_key", name="uq_company_research_key"),)

    id: Mapped[str] = _pk()
    company_key: Mapped[str] = mapped_column(String(300), index=True)
    display_name: Mapped[str] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    findings: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class MemoryRow(Base):
    """One durable lesson. ``kind`` + ``scope`` + ``key`` is the address."""

    __tablename__ = "memory_records"
    __table_args__ = (
        Index("ix_memory_kind_scope_key", "kind", "scope", "key"),
        Index("ix_memory_active", "kind", "superseded_by"),
    )

    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String(32), index=True)
    scope: Mapped[str] = mapped_column(String(200), default="*")
    key: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    level: Mapped[str] = mapped_column(String(32), default="inferred")
    provenance: Mapped[list[Any]] = mapped_column(default=list)
    supporting_observations: Mapped[int] = mapped_column(Integer, default=1)
    contradicting_observations: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(ULID, nullable=True)


class LearningSignalRow(Base):
    """The diff between generated and user-edited material."""

    __tablename__ = "learning_signals"

    id: Mapped[str] = _pk()
    artifact_kind: Mapped[str] = mapped_column(String(40), index=True)
    artifact_id: Mapped[str | None] = mapped_column(ULID, nullable=True)
    target_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    generated_text: Mapped[str] = mapped_column(Text)
    user_text: Mapped[str] = mapped_column(Text)
    edit_kinds: Mapped[list[str]] = mapped_column(default=list)
    job_id: Mapped[str | None] = mapped_column(ULID, nullable=True)
    application_id: Mapped[str | None] = mapped_column(ULID, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(48), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    derived_memory_ids: Mapped[list[str]] = mapped_column(default=list)


class OutcomeRow(Base):
    """An application result. Correlational; no causal claim is stored."""

    __tablename__ = "outcomes"

    id: Mapped[str] = _pk()
    application_id: Mapped[str] = mapped_column(ULID, index=True)
    job_id: Mapped[str] = mapped_column(ULID, index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    occurred_at: Mapped[datetime] = mapped_column(default=utcnow)
    days_to_outcome: Mapped[float | None] = mapped_column(Float, nullable=True)
    fit_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ats: Mapped[str | None] = mapped_column(String(24), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_variant_id: Mapped[str | None] = mapped_column(ULID, nullable=True)
    causation_known: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceStateRow(Base):
    """Per-source enablement and health, so the UI has one place to read."""

    __tablename__ = "source_states"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    health_state: Mapped[str] = mapped_column(String(20), default="unknown")
    health_detail: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_jobs: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class RunRow(Base):
    """A user-visible operation composed of tasks."""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_kind_started", "kind", "started_at"),)

    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String(48), index=True)
    state: Mapped[str] = mapped_column(String(16), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    failures: Mapped[list[str]] = mapped_column(default=list)
    triggered_by: Mapped[str] = mapped_column(String(24), default="cli")


class TaskRow(Base):
    """A durable unit of work. Survives restart; resumable."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_state_scheduled", "state", "scheduled_for", "priority"),
        Index("ix_tasks_idempotency", "idempotency_key"),
    )

    id: Mapped[str] = _pk()
    run_id: Mapped[str | None] = mapped_column(ULID, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    resume_state: Mapped[dict[str, Any]] = mapped_column(default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    attempts: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    scheduled_for: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempted_strategies: Mapped[list[str]] = mapped_column(default=list)


class LLMCallRow(Base):
    """Audit row per model call: cost, latency, failures, prompt version."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_run_started", "run_id", "started_at"),)

    id: Mapped[str] = _pk()
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128))
    prompt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(48), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ULID, nullable=True)
    task_id: Mapped[str | None] = mapped_column(ULID, nullable=True)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=True)
    failure_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_retries: Mapped[int] = mapped_column(Integer, default=0)


__all__ = [
    "ApplicationArtifactRow",
    "ApplicationEventRow",
    "ApplicationRow",
    "ClaimRow",
    "CompanyResearchRow",
    "FitScoreRow",
    "JobRow",
    "JobSourceRow",
    "LLMCallRow",
    "LearningSignalRow",
    "MemoryRow",
    "OutcomeRow",
    "ProfileRow",
    "RunRow",
    "SourceStateRow",
    "TaskRow",
]
