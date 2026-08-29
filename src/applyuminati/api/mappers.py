"""Service read models -> wire DTOs.

Kept in one module so the wire format has exactly one definition point, and so
a field the UI needs cannot quietly be assembled two different ways in two
routers.
"""

from __future__ import annotations

from applyuminati.api.schemas import (
    ActivityItem,
    ApplicationDetail,
    ApplicationEventInfo,
    ApplicationSummary,
    BackendHealthResponse,
    ComponentHealth,
    DashboardResponse,
    FitScoreInfo,
    JobDetail,
    JobSourceInfo,
    JobSummary,
    ProfileResponse,
    RunSummary,
    ScoreDimensionInfo,
    SourceInfo,
)
from applyuminati.core.models.application import allowed_transitions
from applyuminati.core.models.job import AtsVendor, SourceTier
from applyuminati.core.models.scoring import FitScore
from applyuminati.core.models.task import RunRecord
from applyuminati.core.registry import HealthReport
from applyuminati.core.strategy import SearchStrategy
from applyuminati.services.views import (
    ApplicationView,
    BackendHealthView,
    DashboardView,
    JobView,
    ProfileView,
    SourceView,
)


def health_report_to_dto(report: HealthReport, kind: str) -> ComponentHealth:
    return ComponentHealth(
        name=report.plugin,
        kind=kind,
        state=report.state,
        detail=report.detail,
        facts=report.facts,
        latency_ms=report.latency_ms,
    )


def backend_health_to_dto(view: BackendHealthView) -> BackendHealthResponse:
    return BackendHealthResponse(
        sources=[health_report_to_dto(r, "source") for r in view.sources],
        llm=[health_report_to_dto(r, "llm") for r in view.llm],
        browsers=[health_report_to_dto(r, "browser") for r in view.browsers],
        agents=[health_report_to_dto(r, "agent") for r in view.agents],
        email=[health_report_to_dto(r, "email") for r in view.email],
        load_errors=view.load_errors,
    )


def score_to_dto(score: FitScore) -> FitScoreInfo:
    return FitScoreInfo(
        id=score.id,
        overall=score.overall,
        confidence=score.confidence,
        recommendation=score.recommendation,
        explanation=score.explanation,
        baseline_overall=score.baseline_overall,
        scorer_version=score.scorer_version,
        llm_provider=score.llm_provider,
        llm_model=score.llm_model,
        dimensions=[
            ScoreDimensionInfo(
                dimension=d.dimension,
                score=d.score,
                weight=d.weight,
                confidence=d.confidence,
                rationale=d.rationale,
                llm_adjusted=d.llm_adjusted,
            )
            for d in score.dimensions
        ],
        matched_evidence=[m.model_dump(mode="json") for m in score.matched_evidence],
        missing_requirements=[m.model_dump(mode="json") for m in score.missing_requirements],
        uncertainties=score.uncertainties,
        scored_at=score.scored_at,
    )


def _compensation_text(view: JobView) -> str | None:
    comp = view.job.compensation
    if comp is None or not comp.is_specified:
        return None
    if comp.raw_text:
        return comp.raw_text
    low, high = comp.minimum, comp.maximum
    unit = comp.period.value
    if low is not None and high is not None:
        return f"{comp.currency} {low:,.0f}–{high:,.0f} / {unit}"
    value = low if low is not None else high
    return f"{comp.currency} {value:,.0f} / {unit}"


def job_to_summary(view: JobView) -> JobSummary:
    job = view.job
    return JobSummary(
        id=job.id,
        title=job.title,
        company=job.company,
        location=job.locations[0].display() if job.locations else "Unspecified",
        remote_mode=job.remote_mode,
        employment_type=job.employment_type,
        seniority=job.seniority,
        ats=job.ats,
        sources=job.source_slugs,
        canonical_url=job.canonical_url,
        apply_url=job.apply_url,
        compensation=_compensation_text(view),
        posted_at=job.posted_at,
        discovered_at=job.discovered_at,
        freshness_days=round(job.freshness_days(), 2),
        verification=job.verification,
        fit_score=view.score.overall if view.score else None,
        recommendation=view.score.recommendation if view.score else None,
        application_state=view.application_state,
        duplicate_source_count=max(0, len(job.sources) - 1),
    )


def job_to_detail(view: JobView) -> JobDetail:
    job = view.job
    summary = job_to_summary(view)
    actions = (
        [state.value for state in allowed_transitions(view.application_state)]
        if view.application_state
        else []
    )
    return JobDetail(
        **summary.model_dump(),
        description=job.description,
        requirements=job.requirements,
        preferred_qualifications=job.preferred_qualifications,
        skills=job.skills,
        locations=[loc.model_dump(mode="json") for loc in job.locations],
        source_records=[
            JobSourceInfo(
                source=record.source,
                tier=record.tier,
                url=record.url,
                source_job_id=record.source_job_id,
                first_seen_at=record.first_seen_at,
                last_seen_at=record.last_seen_at,
                confidence=record.confidence,
            )
            for record in job.sources
        ],
        score=score_to_dto(view.score) if view.score else None,
        merged_job_ids=job.merged_job_ids,
        available_actions=actions,
    )


def application_to_summary(view: ApplicationView) -> ApplicationSummary:
    app = view.application
    return ApplicationSummary(
        id=app.id,
        job_id=app.job_id,
        job_title=view.job.title,
        company=view.job.company,
        state=app.state,
        fit_score=view.score.overall if view.score else None,
        created_at=app.created_at,
        updated_at=app.updated_at,
        submitted_at=app.submitted_at,
        needs_attention=app.needs_attention,
    )


def application_to_detail(view: ApplicationView) -> ApplicationDetail:
    app = view.application
    summary = application_to_summary(view)
    return ApplicationDetail(
        **summary.model_dump(),
        external_reference=app.external_reference,
        notes=app.notes,
        events=[
            ApplicationEventInfo(
                id=event.id,
                occurred_at=event.occurred_at,
                from_state=event.from_state,
                to_state=event.to_state,
                actor=event.actor.value,
                actor_detail=event.actor_detail,
                reason=event.reason,
                message=event.message,
                failure_category=(
                    event.failure_category.value if event.failure_category else None
                ),
            )
            for event in app.events
        ],
        artifacts=[artifact.model_dump(mode="json") for artifact in app.artifacts],
        allowed_transitions=allowed_transitions(app.state),
    )


def source_to_dto(view: SourceView) -> SourceInfo:
    return SourceInfo(
        slug=view.slug,
        name=view.name,
        description=view.description,
        tier=SourceTier(view.tier),
        ats=AtsVendor(view.ats),
        enabled=view.enabled,
        capabilities=view.capabilities,
        requires_auth=view.requires_auth,
        blocking=view.blocking,
        health=health_report_to_dto(view.health, "source") if view.health else None,
        options=dict(view.options),
        options_schema=dict(view.options_schema) if view.options_schema else None,
        last_run_at=view.last_run_at,
        last_run_jobs=view.last_run_jobs,
        consecutive_failures=view.consecutive_failures,
    )


def run_to_dto(run: RunRecord) -> RunSummary:
    return RunSummary(
        id=run.id,
        kind=run.kind,
        state=run.state.value,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=run.duration_seconds,
        stats=run.stats,
        failures=run.failures,
        triggered_by=run.triggered_by,
    )


def dashboard_to_dto(view: DashboardView) -> DashboardResponse:
    return DashboardResponse(
        total_jobs=view.total_jobs,
        shortlisted=view.shortlisted,
        ready=view.ready,
        submitted=view.submitted,
        needs_attention=view.needs_attention,
        scored=view.scored,
        unscored=view.unscored,
        by_recommendation=view.by_recommendation,
        by_source=view.by_source,
        by_application_state=view.by_application_state,
        recent_activity=[
            ActivityItem(
                at=entry.at,
                kind=entry.kind,
                summary=entry.summary,
                job_id=entry.job_id,
                application_id=entry.application_id,
            )
            for entry in view.recent_activity
        ],
        latest_run=run_to_dto(view.latest_run) if view.latest_run else None,
    )


def profile_to_dto(
    view: ProfileView, *, strategy: SearchStrategy, targets: dict[str, object]
) -> ProfileResponse:
    return ProfileResponse(
        id=view.profile_id,
        label=view.label,
        resume=dict(view.resume),
        name=view.name,
        headline=view.headline,
        email=view.email,
        counts=view.counts,
        targets=targets,
        strategy=strategy,
        claim_levels=view.claim_levels,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


__all__ = [
    "application_to_detail",
    "application_to_summary",
    "backend_health_to_dto",
    "dashboard_to_dto",
    "health_report_to_dto",
    "job_to_detail",
    "job_to_summary",
    "profile_to_dto",
    "run_to_dto",
    "score_to_dto",
    "source_to_dto",
]
