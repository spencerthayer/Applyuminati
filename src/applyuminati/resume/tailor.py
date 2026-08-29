"""Resume tailoring: reorder and select, never invent.

The deterministic path (always runs, works with no LLM at all) reorders work
entries and highlight bullets by relevance to the job's skills and
requirements, selects the most relevant N highlights per role, and surfaces
truthful keywords. Every factual element — employer, title, date, metric — is
preserved verbatim; only ordering and selection change.

The optional LLM path may rewrite *existing* verified claims more clearly.
Its output is always passed through :class:`FabricationGuard`; a hard
violation discards the LLM output and falls back to the deterministic result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from applyuminati.core.logging import get_logger
from applyuminati.core.models.job import Job
from applyuminati.core.models.jsonresume import JsonResume, ResumeWork
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.provenance import EvidenceLink
from applyuminati.resume.evidence import EvidenceIndex
from applyuminati.resume.guard import FabricationGuard, GuardReport

if TYPE_CHECKING:
    from applyuminati.llm.client import LLMClient

log = get_logger(__name__)

__all__ = ["ResumeTailor", "TailorResult"]

_MAX_HIGHLIGHTS_PER_ROLE = 4


@dataclass(slots=True)
class TailorResult:
    resume: JsonResume
    guard: GuardReport
    evidence_links: list[EvidenceLink] = field(default_factory=list)
    used_llm: bool = False


class ResumeTailor:
    """Tailor a resume for a job, deterministically first."""

    def __init__(self, profile: CareerProfile) -> None:
        self._profile = profile
        self._index = EvidenceIndex(profile)
        self._guard = FabricationGuard(profile)

    async def tailor(
        self,
        job: Job,
        *,
        client: LLMClient | None = None,
        allow_llm: bool = True,
    ) -> TailorResult:
        deterministic = self._deterministic(job)
        if not allow_llm or client is None or not client.is_configured:
            return TailorResult(resume=deterministic, guard=self._guard.check(deterministic))

        try:
            llm_resume = await self._llm_rewrite(job, deterministic, client)
        except Exception as exc:  # noqa: BLE001 - LLM failure falls back
            log.warning("tailor.llm_failed", job_id=job.id, error=str(exc))
            return TailorResult(resume=deterministic, guard=self._guard.check(deterministic))

        report = self._guard.check(llm_resume)
        if not report.ok:
            log.warning("tailor.llm_rejected_by_guard", job_id=job.id, violations=len(report.hard_violations))
            return TailorResult(resume=deterministic, guard=self._guard.check(deterministic))
        return TailorResult(resume=llm_resume, guard=report, used_llm=True)

    def _deterministic(self, job: Job) -> JsonResume:
        job_skills = {skill.lower() for skill in job.skills}
        job_req_tokens = {token.lower() for req in job.requirements for token in req.split()}

        def _highlight_score(highlight: str) -> float:
            tokens = {token.lower() for token in highlight.split()}
            return len(tokens & job_skills) * 2 + len(tokens & job_req_tokens)

        reordered_work: list[ResumeWork] = []
        for work in self._profile.resume.work:
            scored_highlights = sorted(
                work.highlights, key=_highlight_score, reverse=True
            )[:_MAX_HIGHLIGHTS_PER_ROLE]
            reordered_work.append(work.model_copy(update={"highlights": scored_highlights}))

        # Reorder roles by the sum of their highlight scores.
        reordered_work.sort(
            key=lambda work: sum(_highlight_score(h) for h in work.highlights), reverse=True
        )

        # Rewrite the summary only if the profile has one; keep it truthful.
        summary = self._profile.resume.basics.summary
        return self._profile.resume.model_copy(update={"work": reordered_work, "basics": self._profile.resume.basics.model_copy(update={"summary": summary})})

    async def _llm_rewrite(
        self, job: Job, deterministic: JsonResume, client: LLMClient
    ) -> JsonResume:
        """Ask the model to rewrite existing bullets, then splice them back in."""
        result, _ = await client.structured(
            "resume.tailor",
            schema=_TailorSchema,
            job_title=job.title,
            company=job.company,
            description_excerpt=(job.description or "")[:2000],
            bullets=[
                {"claim_id": str(i), "text": highlight}
                for i, work in enumerate(deterministic.work)
                for j, highlight in enumerate(work.highlights)
                if highlight
            ],
        )
        rewrites = {item.claim_id: item.text for item in result.bullets}
        new_work: list[ResumeWork] = []
        cursor = 0
        for work in deterministic.work:
            new_highlights: list[str] = []
            for highlight in work.highlights:
                key = str(cursor)
                cursor += 1
                new_highlights.append(rewrites.get(key, highlight))
            new_work.append(work.model_copy(update={"highlights": new_highlights}))
        return deterministic.model_copy(update={"work": new_work})


from pydantic import BaseModel, Field  # noqa: E402


class _TailoredBullet(BaseModel):
    claim_id: str
    text: str


class _TailorSchema(BaseModel):
    bullets: list[_TailoredBullet] = Field(default_factory=list)
