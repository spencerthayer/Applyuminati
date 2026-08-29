"""Row <-> domain-model translation.

The tables in :mod:`applyuminati.db.models` are a *storage* shape: real columns
for anything filtered, sorted or joined on, JSON for everything else. The
Pydantic models in :mod:`applyuminati.core.models` are the *domain* shape. This
module is the only place the two meet, so neither side acquires knowledge of
the other and a column change never leaks into scoring or the API.

Invariants worth knowing before editing:

* JSON columns always hold ``model_dump(mode="json")`` output and are always
  read back through ``Model.model_validate``. The database therefore never
  becomes a second, divergent definition of the domain.
* Every ``*_to_row`` takes an optional existing ``row``, so the create and the
  update path share one field list and updating a subset of columns by
  accident is impossible.
* Mappers never touch a session, never read the clock, and never trigger a
  lazy load: related collections are passed in by the repository that already
  loaded them. ``updated_at`` is copied verbatim from the domain model; the
  repository owns bumping it on the write path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from applyuminati.core.models.application import (
    Application,
    ApplicationArtifact,
    ApplicationEvent,
)
from applyuminati.core.models.job import Job, JobSourceRecord
from applyuminati.core.models.memory import LearningSignal, MemoryRecord, OutcomeRecord
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.research import CompanyResearch
from applyuminati.core.models.scoring import FitScore
from applyuminati.core.models.task import RunRecord, TaskRecord
from applyuminati.core.provenance import Claim
from applyuminati.db.models import (
    ApplicationArtifactRow,
    ApplicationEventRow,
    ApplicationRow,
    ClaimRow,
    CompanyResearchRow,
    FitScoreRow,
    JobRow,
    JobSourceRow,
    LearningSignalRow,
    LLMCallRow,
    MemoryRow,
    OutcomeRow,
    ProfileRow,
    RunRow,
    TaskRow,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime


# ---------------------------------------------------------------------------
# profile + claims
# ---------------------------------------------------------------------------

#: Profile fields promoted to their own column (or their own table), and
#: therefore excluded from the ``profile_data`` JSON blob.
_PROFILE_COLUMN_FIELDS = frozenset({"id", "label", "resume", "claims", "created_at", "updated_at"})


def profile_to_row(
    profile: CareerProfile, *, row: ProfileRow | None = None, is_active: bool = True
) -> ProfileRow:
    """Project a profile onto its row. Claims are written separately."""
    target = row if row is not None else ProfileRow(id=profile.id)
    target.label = profile.label
    target.resume = profile.resume.model_dump(mode="json")
    target.profile_data = profile.model_dump(mode="json", exclude=set(_PROFILE_COLUMN_FIELDS))
    target.is_active = is_active
    target.created_at = profile.created_at
    target.updated_at = profile.updated_at
    return target


def row_to_profile(row: ProfileRow, claims: Sequence[ClaimRow]) -> CareerProfile:
    """Rebuild a profile from its row plus its already-loaded claim rows."""
    payload: dict[str, Any] = dict(row.profile_data)
    payload["id"] = row.id
    payload["label"] = row.label
    payload["resume"] = row.resume
    payload["claims"] = [row_to_claim(claim) for claim in claims]
    payload["created_at"] = row.created_at
    payload["updated_at"] = row.updated_at
    return CareerProfile.model_validate(payload)


def claim_to_row(claim: Claim, profile_id: str, *, row: ClaimRow | None = None) -> ClaimRow:
    """Project one claim onto its row."""
    payload = claim.model_dump(mode="json")
    target = row if row is not None else ClaimRow(id=claim.id)
    target.profile_id = profile_id
    target.statement = claim.statement
    target.level = claim.level.value
    target.tags = payload["tags"]
    target.data = payload["data"]
    target.provenance = payload["provenance"]
    target.created_at = claim.created_at
    target.updated_at = claim.updated_at
    target.superseded_by = claim.superseded_by
    return target


def row_to_claim(row: ClaimRow) -> Claim:
    """Rebuild one claim from its row."""
    return Claim.model_validate(
        {
            "id": row.id,
            "statement": row.statement,
            "level": row.level,
            "provenance": row.provenance,
            "data": row.data,
            "tags": row.tags,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "superseded_by": row.superseded_by,
        }
    )


# ---------------------------------------------------------------------------
# job + source records
# ---------------------------------------------------------------------------

#: Columns whose value belongs to whichever source sits closest to the
#: employer. :meth:`applyuminati.db.repositories.jobs.JobRepository.upsert` uses
#: this list so a higher-tier record can win a merge without restating the
#: field set in the repository.
JOB_CANONICAL_COLUMNS: tuple[str, ...] = (
    "canonical_url",
    "apply_url",
    "company",
    "company_key",
    "company_domain",
    "title",
    "title_raw",
    "title_key",
    "department",
    "seniority",
    "remote_mode",
    "employment_type",
    "comp_min_annual",
    "comp_max_annual",
    "comp_currency",
    "locations",
    "compensation",
    "description",
    "requirements",
    "preferred_qualifications",
    "skills",
    "posted_at",
    "valid_through",
    "ats",
)


def job_canonical_values(job: Job) -> dict[str, Any]:
    """Column values a source asserts about the posting itself.

    The denormalised ``comp_*`` columns come from
    :meth:`~applyuminati.core.models.common.Compensation.annualised`, so range
    queries work across pay periods without the caller knowing the posting's
    period.
    """
    payload = job.model_dump(mode="json")
    low, high = job.compensation.annualised() if job.compensation else (None, None)
    return {
        "canonical_url": job.canonical_url,
        "apply_url": job.apply_url,
        "company": job.company,
        "company_key": job.company_key,
        "company_domain": job.company_domain,
        "title": job.title,
        "title_raw": job.title_raw,
        "title_key": job.title_key,
        "department": job.department,
        "seniority": job.seniority.value,
        "remote_mode": job.remote_mode.value,
        "employment_type": job.employment_type.value,
        "comp_min_annual": low,
        "comp_max_annual": high,
        "comp_currency": job.compensation.currency if job.compensation else None,
        "locations": payload["locations"],
        "compensation": payload["compensation"],
        "description": job.description,
        "requirements": payload["requirements"],
        "preferred_qualifications": payload["preferred_qualifications"],
        "skills": payload["skills"],
        "posted_at": job.posted_at,
        "valid_through": job.valid_through,
        "ats": job.ats.value,
    }


def job_lifecycle_values(job: Job) -> dict[str, Any]:
    """Column values owned by the pipeline rather than by a source."""
    return {
        "identity_key": job.identity_key,
        "discovered_at": job.discovered_at,
        "last_seen_at": job.last_seen_at,
        "last_verified_at": job.last_verified_at,
        "verification": job.verification.value,
        "stage": job.stage.value,
        "merged_job_ids": list(job.merged_job_ids),
    }


def job_to_row(job: Job, *, row: JobRow | None = None) -> JobRow:
    """Project a job onto its row. Source records are written separately."""
    target = row if row is not None else JobRow(id=job.id)
    for column, value in job_canonical_values(job).items():
        setattr(target, column, value)
    for column, value in job_lifecycle_values(job).items():
        setattr(target, column, value)
    return target


def row_to_job(row: JobRow, sources: Sequence[JobSourceRow]) -> Job:
    """Rebuild a job from its row plus its already-loaded source rows."""
    return Job.model_validate(
        {
            "id": row.id,
            "identity_key": row.identity_key,
            "canonical_url": row.canonical_url,
            "apply_url": row.apply_url,
            "company": row.company,
            "company_key": row.company_key,
            "company_domain": row.company_domain,
            "title": row.title,
            "title_raw": row.title_raw,
            "title_key": row.title_key,
            "department": row.department,
            "seniority": row.seniority,
            "locations": row.locations,
            "remote_mode": row.remote_mode,
            "employment_type": row.employment_type,
            "compensation": row.compensation,
            "description": row.description,
            "requirements": row.requirements,
            "preferred_qualifications": row.preferred_qualifications,
            "skills": row.skills,
            "posted_at": row.posted_at,
            "discovered_at": row.discovered_at,
            "last_seen_at": row.last_seen_at,
            "last_verified_at": row.last_verified_at,
            "valid_through": row.valid_through,
            "verification": row.verification,
            "stage": row.stage,
            "ats": row.ats,
            "sources": [row_to_source_record(source) for source in sources],
            "merged_job_ids": row.merged_job_ids,
        }
    )


def source_record_to_row(
    record: JobSourceRecord, job_id: str, *, row: JobSourceRow | None = None
) -> JobSourceRow:
    """Project one source record onto its row."""
    payload = record.model_dump(mode="json")
    target = row if row is not None else JobSourceRow(id=record.id)
    target.job_id = job_id
    target.source = record.source
    target.tier = record.tier.value
    target.source_job_id = record.source_job_id
    target.url = record.url
    target.canonical_url = record.canonical_url
    target.apply_url = record.apply_url
    target.first_seen_at = record.first_seen_at
    target.last_seen_at = record.last_seen_at
    target.confidence = record.confidence
    target.payload_hash = record.payload_hash
    target.raw = payload["raw"]
    return target


def row_to_source_record(row: JobSourceRow) -> JobSourceRecord:
    """Rebuild one source record from its row."""
    return JobSourceRecord.model_validate(
        {
            "id": row.id,
            "source": row.source,
            "tier": row.tier,
            "source_job_id": row.source_job_id,
            "url": row.url,
            "canonical_url": row.canonical_url,
            "apply_url": row.apply_url,
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "confidence": row.confidence,
            "payload_hash": row.payload_hash,
            "raw": row.raw,
        }
    )


# ---------------------------------------------------------------------------
# fit scores
# ---------------------------------------------------------------------------


def score_to_row(score: FitScore, *, row: FitScoreRow | None = None) -> FitScoreRow:
    """Project a fit score onto its row."""
    payload = score.model_dump(mode="json")
    target = row if row is not None else FitScoreRow(id=score.id)
    target.job_id = score.job_id
    target.profile_id = score.profile_id
    target.overall = score.overall
    target.confidence = score.confidence
    target.recommendation = score.recommendation.value
    target.baseline_overall = score.baseline_overall
    target.scorer_version = score.scorer_version
    target.llm_provider = score.llm_provider
    target.llm_model = score.llm_model
    target.llm_prompt_version = score.llm_prompt_version
    target.explanation = score.explanation
    target.dimensions = payload["dimensions"]
    target.matched_evidence = payload["matched_evidence"]
    target.missing_requirements = payload["missing_requirements"]
    target.uncertainties = payload["uncertainties"]
    target.scored_at = score.scored_at
    return target


def row_to_score(row: FitScoreRow) -> FitScore:
    """Rebuild a fit score from its row."""
    return FitScore.model_validate(
        {
            "id": row.id,
            "job_id": row.job_id,
            "profile_id": row.profile_id,
            "overall": row.overall,
            "confidence": row.confidence,
            "recommendation": row.recommendation,
            "dimensions": row.dimensions,
            "matched_evidence": row.matched_evidence,
            "missing_requirements": row.missing_requirements,
            "uncertainties": row.uncertainties,
            "explanation": row.explanation,
            "baseline_overall": row.baseline_overall,
            "scorer_version": row.scorer_version,
            "llm_provider": row.llm_provider,
            "llm_model": row.llm_model,
            "llm_prompt_version": row.llm_prompt_version,
            "scored_at": row.scored_at,
        }
    )


# ---------------------------------------------------------------------------
# applications
# ---------------------------------------------------------------------------


def application_to_row(
    application: Application, *, row: ApplicationRow | None = None
) -> ApplicationRow:
    """Project an application onto its row. Events and artifacts are separate."""
    target = row if row is not None else ApplicationRow(id=application.id)
    target.job_id = application.job_id
    target.profile_id = application.profile_id
    target.state = application.state.value
    target.created_at = application.created_at
    target.updated_at = application.updated_at
    target.submitted_at = application.submitted_at
    target.external_reference = application.external_reference
    target.fit_score_id = application.fit_score_id
    target.submission_fingerprint = application.submission_fingerprint
    target.notes = application.notes
    return target


def row_to_application(
    row: ApplicationRow,
    events: Sequence[ApplicationEventRow],
    artifacts: Sequence[ApplicationArtifactRow],
) -> Application:
    """Rebuild an application from its row plus its already-loaded children."""
    return Application.model_validate(
        {
            "id": row.id,
            "job_id": row.job_id,
            "profile_id": row.profile_id,
            "state": row.state,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "submitted_at": row.submitted_at,
            "external_reference": row.external_reference,
            "fit_score_id": row.fit_score_id,
            "submission_fingerprint": row.submission_fingerprint,
            "notes": row.notes,
            "events": [row_to_event(event) for event in events],
            "artifacts": [row_to_artifact(artifact) for artifact in artifacts],
        }
    )


def event_to_row(
    event: ApplicationEvent, *, row: ApplicationEventRow | None = None
) -> ApplicationEventRow:
    """Project one application event onto its row."""
    payload = event.model_dump(mode="json")
    target = row if row is not None else ApplicationEventRow(id=event.id)
    target.application_id = event.application_id
    target.occurred_at = event.occurred_at
    target.from_state = event.from_state.value if event.from_state else None
    target.to_state = event.to_state.value if event.to_state else None
    target.actor = event.actor.value
    target.actor_detail = event.actor_detail
    target.reason = event.reason
    target.message = event.message
    target.data = payload["data"]
    target.failure_category = event.failure_category.value if event.failure_category else None
    target.run_id = event.run_id
    target.task_id = event.task_id
    return target


def row_to_event(row: ApplicationEventRow) -> ApplicationEvent:
    """Rebuild one application event from its row."""
    return ApplicationEvent.model_validate(
        {
            "id": row.id,
            "application_id": row.application_id,
            "occurred_at": row.occurred_at,
            "from_state": row.from_state,
            "to_state": row.to_state,
            "actor": row.actor,
            "actor_detail": row.actor_detail,
            "reason": row.reason,
            "message": row.message,
            "data": row.data,
            "failure_category": row.failure_category,
            "run_id": row.run_id,
            "task_id": row.task_id,
        }
    )


def artifact_to_row(
    artifact: ApplicationArtifact,
    application_id: str,
    *,
    row: ApplicationArtifactRow | None = None,
) -> ApplicationArtifactRow:
    """Project one application artifact onto its row."""
    payload = artifact.model_dump(mode="json")
    target = row if row is not None else ApplicationArtifactRow(id=artifact.id)
    target.application_id = application_id
    target.kind = artifact.kind
    target.relative_path = artifact.relative_path
    target.content_type = artifact.content_type
    target.bytes_written = artifact.bytes_written
    target.created_at = artifact.created_at
    target.evidence_claim_ids = payload["evidence_claim_ids"]
    return target


def row_to_artifact(row: ApplicationArtifactRow) -> ApplicationArtifact:
    """Rebuild one application artifact from its row."""
    return ApplicationArtifact.model_validate(
        {
            "id": row.id,
            "kind": row.kind,
            "relative_path": row.relative_path,
            "content_type": row.content_type,
            "bytes_written": row.bytes_written,
            "created_at": row.created_at,
            "evidence_claim_ids": row.evidence_claim_ids,
        }
    )


# ---------------------------------------------------------------------------
# memory, learning signals, outcomes
# ---------------------------------------------------------------------------


def memory_to_row(record: MemoryRecord, *, row: MemoryRow | None = None) -> MemoryRow:
    """Project one memory record onto its row."""
    payload = record.model_dump(mode="json")
    target = row if row is not None else MemoryRow(id=record.id)
    target.kind = record.kind.value
    target.scope = record.scope
    target.key = record.key
    target.content = record.content
    target.data = payload["data"]
    target.level = record.level.value
    target.provenance = payload["provenance"]
    target.supporting_observations = record.supporting_observations
    target.contradicting_observations = record.contradicting_observations
    target.created_at = record.created_at
    target.updated_at = record.updated_at
    target.last_used_at = record.last_used_at
    target.expires_at = record.expires_at
    target.superseded_by = record.superseded_by
    return target


def row_to_memory(row: MemoryRow) -> MemoryRecord:
    """Rebuild one memory record from its row."""
    return MemoryRecord.model_validate(
        {
            "id": row.id,
            "kind": row.kind,
            "scope": row.scope,
            "key": row.key,
            "content": row.content,
            "data": row.data,
            "level": row.level,
            "provenance": row.provenance,
            "supporting_observations": row.supporting_observations,
            "contradicting_observations": row.contradicting_observations,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "last_used_at": row.last_used_at,
            "expires_at": row.expires_at,
            "superseded_by": row.superseded_by,
        }
    )


def signal_to_row(
    signal: LearningSignal, *, row: LearningSignalRow | None = None
) -> LearningSignalRow:
    """Project one learning signal onto its row."""
    payload = signal.model_dump(mode="json")
    target = row if row is not None else LearningSignalRow(id=signal.id)
    target.artifact_kind = signal.artifact_kind
    target.artifact_id = signal.artifact_id
    target.target_path = signal.target_path
    target.generated_text = signal.generated_text
    target.user_text = signal.user_text
    target.edit_kinds = payload["edit_kinds"]
    target.job_id = signal.job_id
    target.application_id = signal.application_id
    target.prompt_version = signal.prompt_version
    target.llm_model = signal.llm_model
    target.created_at = signal.created_at
    target.derived_memory_ids = payload["derived_memory_ids"]
    return target


def row_to_signal(row: LearningSignalRow) -> LearningSignal:
    """Rebuild one learning signal from its row."""
    return LearningSignal.model_validate(
        {
            "id": row.id,
            "artifact_kind": row.artifact_kind,
            "artifact_id": row.artifact_id,
            "target_path": row.target_path,
            "generated_text": row.generated_text,
            "user_text": row.user_text,
            "edit_kinds": row.edit_kinds,
            "job_id": row.job_id,
            "application_id": row.application_id,
            "prompt_version": row.prompt_version,
            "llm_model": row.llm_model,
            "created_at": row.created_at,
            "derived_memory_ids": row.derived_memory_ids,
        }
    )


def outcome_to_row(outcome: OutcomeRecord, *, row: OutcomeRow | None = None) -> OutcomeRow:
    """Project one outcome record onto its row."""
    target = row if row is not None else OutcomeRow(id=outcome.id)
    target.application_id = outcome.application_id
    target.job_id = outcome.job_id
    target.outcome = outcome.outcome
    target.occurred_at = outcome.occurred_at
    target.days_to_outcome = outcome.days_to_outcome
    target.fit_score = outcome.fit_score
    target.ats = outcome.ats
    target.source = outcome.source
    target.resume_variant_id = outcome.resume_variant_id
    target.causation_known = outcome.causation_known
    target.notes = outcome.notes
    return target


def row_to_outcome(row: OutcomeRow) -> OutcomeRecord:
    """Rebuild one outcome record from its row."""
    return OutcomeRecord.model_validate(
        {
            "id": row.id,
            "application_id": row.application_id,
            "job_id": row.job_id,
            "outcome": row.outcome,
            "occurred_at": row.occurred_at,
            "days_to_outcome": row.days_to_outcome,
            "fit_score": row.fit_score,
            "ats": row.ats,
            "source": row.source,
            "resume_variant_id": row.resume_variant_id,
            "causation_known": row.causation_known,
            "notes": row.notes,
        }
    )


# ---------------------------------------------------------------------------
# company research
# ---------------------------------------------------------------------------


def research_to_row(
    research: CompanyResearch, *, row: CompanyResearchRow | None = None
) -> CompanyResearchRow:
    """Project company research onto its row."""
    payload = research.model_dump(mode="json")
    target = row if row is not None else CompanyResearchRow(id=research.id)
    target.company_key = research.company_key
    target.display_name = research.display_name
    target.domain = research.domain
    target.findings = payload["findings"]
    target.created_at = research.created_at
    target.updated_at = research.updated_at
    return target


def row_to_research(row: CompanyResearchRow) -> CompanyResearch:
    """Rebuild company research from its row."""
    return CompanyResearch.model_validate(
        {
            "id": row.id,
            "company_key": row.company_key,
            "display_name": row.display_name,
            "domain": row.domain,
            "findings": row.findings,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


# ---------------------------------------------------------------------------
# tasks and runs
# ---------------------------------------------------------------------------


def task_to_row(task: TaskRecord, *, row: TaskRow | None = None) -> TaskRow:
    """Project one task onto its row."""
    payload = task.model_dump(mode="json")
    target = row if row is not None else TaskRow(id=task.id)
    target.run_id = task.run_id
    target.kind = task.kind
    target.state = task.state.value
    target.payload = payload["payload"]
    target.result = payload["result"]
    target.resume_state = payload["resume_state"]
    target.idempotency_key = task.idempotency_key
    target.priority = task.priority
    target.max_attempts = task.max_attempts
    target.attempts = payload["attempts"]
    target.created_at = task.created_at
    target.updated_at = task.updated_at
    target.scheduled_for = task.scheduled_for
    target.started_at = task.started_at
    target.finished_at = task.finished_at
    target.lease_expires_at = task.lease_expires_at
    target.failure_category = task.failure_category.value if task.failure_category else None
    target.failure_message = task.failure_message
    target.attempted_strategies = payload["attempted_strategies"]
    return target


def row_to_task(row: TaskRow) -> TaskRecord:
    """Rebuild one task from its row."""
    return TaskRecord.model_validate(
        {
            "id": row.id,
            "run_id": row.run_id,
            "kind": row.kind,
            "state": row.state,
            "payload": row.payload,
            "result": row.result,
            "resume_state": row.resume_state,
            "idempotency_key": row.idempotency_key,
            "priority": row.priority,
            "max_attempts": row.max_attempts,
            "attempts": row.attempts,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "scheduled_for": row.scheduled_for,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "lease_expires_at": row.lease_expires_at,
            "failure_category": row.failure_category,
            "failure_message": row.failure_message,
            "attempted_strategies": row.attempted_strategies,
        }
    )


def run_to_row(run: RunRecord, *, row: RunRow | None = None) -> RunRow:
    """Project one run onto its row."""
    payload = run.model_dump(mode="json")
    target = row if row is not None else RunRow(id=run.id)
    target.kind = run.kind
    target.state = run.state.value
    target.started_at = run.started_at
    target.finished_at = run.finished_at
    target.parameters = payload["parameters"]
    target.stats = payload["stats"]
    target.failures = payload["failures"]
    target.triggered_by = run.triggered_by
    return target


def row_to_run(row: RunRow) -> RunRecord:
    """Rebuild one run from its row."""
    return RunRecord.model_validate(
        {
            "id": row.id,
            "kind": row.kind,
            "state": row.state,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "parameters": row.parameters,
            "stats": row.stats,
            "failures": row.failures,
            "triggered_by": row.triggered_by,
        }
    )


# ---------------------------------------------------------------------------
# llm call audit
# ---------------------------------------------------------------------------


class LLMCallLike(Protocol):
    """Structural view of ``applyuminati.llm.base.LLMCallRecord``.

    Declared structurally on purpose: ``applyuminati.db`` sits *below*
    ``applyuminati.llm`` in the dependency order, so this layer must not import
    the LLM package. The audit trail only ever reads these attributes.
    """

    id: str
    provider: str
    model: str
    prompt_id: str | None
    prompt_version: str | None
    run_id: str | None
    task_id: str | None
    started_at: datetime
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    succeeded: bool
    failure_category: str | None
    failure_message: str | None
    validation_retries: int


def llm_call_to_row(call: LLMCallLike, *, row: LLMCallRow | None = None) -> LLMCallRow:
    """Project one LLM call audit record onto its row."""
    target = row if row is not None else LLMCallRow(id=call.id)
    target.provider = call.provider
    target.model = call.model
    target.prompt_id = call.prompt_id
    target.prompt_version = call.prompt_version
    target.run_id = call.run_id
    target.task_id = call.task_id
    target.started_at = call.started_at
    target.latency_ms = call.latency_ms
    target.input_tokens = call.input_tokens
    target.output_tokens = call.output_tokens
    target.estimated_cost_usd = call.estimated_cost_usd
    target.succeeded = call.succeeded
    target.failure_category = call.failure_category
    target.failure_message = call.failure_message
    target.validation_retries = call.validation_retries
    return target


__all__ = [
    "JOB_CANONICAL_COLUMNS",
    "LLMCallLike",
    "application_to_row",
    "artifact_to_row",
    "claim_to_row",
    "event_to_row",
    "job_canonical_values",
    "job_lifecycle_values",
    "job_to_row",
    "llm_call_to_row",
    "memory_to_row",
    "outcome_to_row",
    "profile_to_row",
    "research_to_row",
    "row_to_application",
    "row_to_artifact",
    "row_to_claim",
    "row_to_event",
    "row_to_job",
    "row_to_memory",
    "row_to_outcome",
    "row_to_profile",
    "row_to_research",
    "row_to_run",
    "row_to_score",
    "row_to_signal",
    "row_to_source_record",
    "row_to_task",
    "run_to_row",
    "score_to_row",
    "signal_to_row",
    "source_record_to_row",
    "task_to_row",
]
