"""Scoring orchestration.

The deterministic engine always runs. The LLM pass is strictly optional
enrichment on top, gated on a provider being configured and the caller asking
for it — so the product ranks jobs perfectly well with no API key at all.

Scoring also drives the first application transition: a scored job moves its
application from ``discovered`` to ``shortlisted`` or ``skipped`` according to
the strategy thresholds, and the reason is recorded in the event log.
"""

from __future__ import annotations

from collections.abc import Sequence

from applyuminati.applications.machine import ApplicationMachine
from applyuminati.core.clock import utcnow
from applyuminati.core.errors import ApplyuminatiError
from applyuminati.core.logging import bound_context, get_logger
from applyuminati.core.models.application import ActorKind, ApplicationState
from applyuminati.core.models.job import Job, PipelineStage
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.scoring import FitScore, Recommendation
from applyuminati.core.models.task import RunRecord, RunState
from applyuminati.core.settings import Settings
from applyuminati.llm.client import LLMClient
from applyuminati.scoring.engine import score_job
from applyuminati.scoring.llm_pass import enrich_score
from applyuminati.services.container import Repositories
from applyuminati.services.profile_service import ProfileService

log = get_logger(__name__)

#: Recommendation -> the application state a fresh scoring pass moves towards.
_TARGET_STATE: dict[Recommendation, ApplicationState] = {
    Recommendation.APPLY: ApplicationState.SHORTLISTED,
    Recommendation.INVESTIGATE: ApplicationState.SHORTLISTED,
    Recommendation.SKIP: ApplicationState.SKIPPED,
}


class ScoringService:
    def __init__(self, repos: Repositories, settings: Settings, llm: LLMClient | None = None) -> None:
        self._repos = repos
        self._settings = settings
        self._llm = llm
        self._profiles = ProfileService(repos)
        self._machine = ApplicationMachine()

    async def score_jobs(
        self,
        *,
        job_ids: Sequence[str] | None = None,
        rescore: bool = False,
        use_llm: bool = False,
        limit: int = 100,
        triggered_by: str = "api",
    ) -> RunRecord:
        profile = await self._profiles.get()
        run = RunRecord(
            kind="scoring",
            triggered_by=triggered_by,
            parameters={"rescore": rescore, "use_llm": use_llm, "limit": limit},
        )
        run = await self._repos.runs.create(run)

        jobs = await self._select_jobs(job_ids, profile.id, rescore=rescore, limit=limit)
        run.bump("jobs_selected", len(jobs))

        llm_enabled = use_llm and self._llm is not None and self._llm.is_configured
        if use_llm and not llm_enabled:
            run.failures.append(
                "LLM enrichment requested but no provider is configured; "
                "deterministic scores were produced instead"
            )

        with bound_context(run_id=run.id, kind="scoring"):
            for job in jobs:
                try:
                    score = await self._score_one(job, profile, use_llm=llm_enabled, run_id=run.id)
                except ApplyuminatiError as exc:
                    run.bump("failed")
                    run.failures.append(f"{job.id}: {exc.message}")
                    log.warning("scoring.job_failed", job_id=job.id, error=exc.code)
                    continue
                run.bump("scored")
                run.bump(f"recommendation.{score.recommendation.value}")

        run.finished_at = utcnow()
        run.state = RunState.PARTIAL if run.failures else RunState.SUCCEEDED
        if run.stats.get("scored", 0) == 0 and run.failures:
            run.state = RunState.FAILED
        return await self._repos.runs.save(run)

    async def score_one(self, job_id: str, *, use_llm: bool = False) -> FitScore:
        """Score a single job. Used by the CLI and the job detail page."""
        profile = await self._profiles.get()
        job = await self._repos.jobs.get(job_id)
        if job is None:
            from applyuminati.core.errors import NotFoundError

            raise NotFoundError(f"job {job_id} not found", code="resource_gone.job")
        llm_enabled = use_llm and self._llm is not None and self._llm.is_configured
        return await self._score_one(job, profile, use_llm=llm_enabled, run_id=None)

    # -- internals --------------------------------------------------------

    async def _select_jobs(
        self,
        job_ids: Sequence[str] | None,
        profile_id: str,
        *,
        rescore: bool,
        limit: int,
    ) -> list[Job]:
        if job_ids:
            resolved = [await self._repos.jobs.get(job_id) for job_id in job_ids]
            return [job for job in resolved if job is not None]
        jobs, _ = await self._repos.jobs.list(
            has_score=None if rescore else False,
            limit=limit,
            sort="discovered_at",
            descending=True,
        )
        if rescore:
            return jobs
        existing = await self._repos.scores.latest_map([job.id for job in jobs], profile_id)
        return [job for job in jobs if job.id not in existing]

    async def _score_one(
        self,
        job: Job,
        profile: CareerProfile,
        *,
        use_llm: bool,
        run_id: str | None,
    ) -> FitScore:
        score = score_job(job, profile, profile.strategy)
        if use_llm and self._llm is not None:
            score = await enrich_score(score, job, profile, self._llm)

        score = await self._repos.scores.add(score)
        await self._repos.jobs.set_stage(job.id, PipelineStage.SCORED)
        await self._apply_recommendation(job, profile, score, run_id=run_id)
        log.info(
            "scoring.job_scored",
            job_id=job.id,
            overall=round(score.overall, 3),
            recommendation=score.recommendation.value,
            llm=use_llm,
        )
        return score

    async def _apply_recommendation(
        self,
        job: Job,
        profile: CareerProfile,
        score: FitScore,
        *,
        run_id: str | None,
    ) -> None:
        """Move the application to match the recommendation, if legal.

        Never regresses an application the user has already advanced: a job
        that is ``preparing`` does not fall back to ``shortlisted`` because a
        re-score arrived.
        """
        application = await self._repos.applications.ensure(job.id, profile.id)
        application.fit_score_id = score.id
        target = _TARGET_STATE[score.recommendation]

        advanced_states = {
            ApplicationState.PREPARING,
            ApplicationState.READY,
            ApplicationState.APPLYING,
        }
        if application.state in advanced_states or application.already_submitted:
            await self._repos.applications.save(application)
            return

        if application.state is not target:
            try:
                event = self._machine.transition(
                    application,
                    target,
                    actor=ActorKind.SYSTEM,
                    actor_detail=score.scorer_version,
                    reason=f"score.{score.recommendation.value}",
                    message=score.explanation[:500] or None,
                    data={
                        "overall": round(score.overall, 4),
                        "confidence": round(score.confidence, 4),
                        "hard_blockers": [m.requirement for m in score.hard_blockers],
                    },
                    run_id=run_id,
                )
            except ApplyuminatiError as exc:
                log.warning(
                    "scoring.transition_rejected",
                    job_id=job.id,
                    from_state=application.state.value,
                    to_state=target.value,
                    error=exc.code,
                )
            else:
                await self._repos.applications.append_event(event)

        await self._repos.applications.save(application)


__all__ = ["ScoringService"]
