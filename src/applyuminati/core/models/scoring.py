"""Fit scoring results.

A score is a **structured, inspectable object**, never a single opaque number
an LLM produced. Deterministic dimension scores are always computed; an
optional LLM pass may adjust dimensions within bounded limits and add
qualitative findings, but the aggregation maths stays in Python.

``skip`` is a first-class recommendation, and hard blockers cap the overall
score regardless of how well everything else matched.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.provenance import Confidence


class ScoreDimension(StrEnum):
    """The axes a job is judged on. Each is scored 0..1 independently."""

    TITLE_MATCH = "title_match"
    SENIORITY_MATCH = "seniority_match"
    REQUIRED_SKILLS = "required_skills"
    PREFERRED_SKILLS = "preferred_skills"
    DEMONSTRATED_EXPERIENCE = "demonstrated_experience"
    DOMAIN_OVERLAP = "domain_overlap"
    COMPENSATION = "compensation"
    LOCATION = "location"
    EMPLOYMENT_TYPE = "employment_type"
    WORK_AUTHORIZATION = "work_authorization"
    USER_PREFERENCE = "user_preference"


#: Default weights. Sum is normalised at aggregation time, so tuning one
#: weight never silently rescales the others.
DEFAULT_WEIGHTS: dict[ScoreDimension, float] = {
    ScoreDimension.TITLE_MATCH: 0.18,
    ScoreDimension.SENIORITY_MATCH: 0.10,
    ScoreDimension.REQUIRED_SKILLS: 0.20,
    ScoreDimension.PREFERRED_SKILLS: 0.06,
    ScoreDimension.DEMONSTRATED_EXPERIENCE: 0.14,
    ScoreDimension.DOMAIN_OVERLAP: 0.06,
    ScoreDimension.COMPENSATION: 0.08,
    ScoreDimension.LOCATION: 0.08,
    ScoreDimension.EMPLOYMENT_TYPE: 0.03,
    ScoreDimension.WORK_AUTHORIZATION: 0.04,
    ScoreDimension.USER_PREFERENCE: 0.03,
}


class Recommendation(StrEnum):
    APPLY = "apply"
    INVESTIGATE = "investigate"
    SKIP = "skip"


class BlockerSeverity(StrEnum):
    """How badly a missing requirement counts against the job."""

    #: Disqualifying and not fixable by the candidate (e.g. no work authorisation).
    HARD = "hard"
    #: Substantially reduces fit but an application is still defensible.
    SIGNIFICANT = "significant"
    #: Worth noting; small score effect.
    MINOR = "minor"


class DimensionScore(BaseModel):
    """One axis of the fit assessment."""

    model_config = ConfigDict(extra="forbid")

    dimension: ScoreDimension
    score: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=0.0)
    #: How sure we are of *this dimension*, e.g. low when a posting omits pay.
    confidence: Confidence = 1.0
    #: One line the user can read: "3 of 5 required skills evidenced".
    rationale: str = ""
    #: Claim ids from the profile that justified this score.
    evidence_claim_ids: list[str] = Field(default_factory=list)
    #: True when a model adjusted the deterministic value.
    llm_adjusted: bool = False

    @property
    def weighted(self) -> float:
        return self.score * self.weight


class MissingRequirement(BaseModel):
    """Something the posting asks for that the profile does not evidence.

    Gaps (absent capability) and risks (present but weakly evidenced) are kept
    distinct so the UI can say which is which.
    """

    model_config = ConfigDict(extra="forbid")

    requirement: str
    severity: BlockerSeverity = BlockerSeverity.SIGNIFICANT
    #: ``True`` when the profile has *some* related evidence but not enough.
    partially_evidenced: bool = False
    note: str | None = None


class MatchedEvidence(BaseModel):
    """A concrete link between a posting requirement and profile evidence."""

    model_config = ConfigDict(extra="forbid")

    requirement: str
    claim_id: str | None = None
    excerpt: str | None = None
    strength: Confidence = 1.0


class FitScore(BaseModel):
    """The complete, inspectable verdict for one job against one profile."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    job_id: str
    profile_id: str

    overall: float = Field(ge=0.0, le=1.0)
    #: Confidence in the *score itself*, driven by how much data was available.
    confidence: Confidence = 0.5
    recommendation: Recommendation = Recommendation.INVESTIGATE

    dimensions: list[DimensionScore] = Field(default_factory=list)
    matched_evidence: list[MatchedEvidence] = Field(default_factory=list)
    missing_requirements: list[MissingRequirement] = Field(default_factory=list)
    #: Things we could not determine, stated plainly instead of assumed.
    uncertainties: list[str] = Field(default_factory=list)
    #: Human-readable summary. Never the only output.
    explanation: str = ""

    #: Deterministic-only score, retained so an LLM pass is auditable.
    baseline_overall: float | None = None
    scorer_version: str = "baseline/1"
    #: Set when an LLM enrichment pass ran.
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_prompt_version: str | None = None

    scored_at: datetime = Field(default_factory=utcnow)

    @property
    def hard_blockers(self) -> list[MissingRequirement]:
        return [m for m in self.missing_requirements if m.severity is BlockerSeverity.HARD]

    @property
    def has_hard_blocker(self) -> bool:
        return any(m.severity is BlockerSeverity.HARD for m in self.missing_requirements)

    def dimension(self, dimension: ScoreDimension) -> DimensionScore | None:
        return next((d for d in self.dimensions if d.dimension is dimension), None)

    def top_dimensions(self, limit: int = 3) -> list[DimensionScore]:
        return sorted(self.dimensions, key=lambda d: d.weighted, reverse=True)[:limit]

    def weakest_dimensions(self, limit: int = 3) -> list[DimensionScore]:
        scored = [d for d in self.dimensions if d.weight > 0]
        return sorted(scored, key=lambda d: d.score)[:limit]


__all__ = [
    "DEFAULT_WEIGHTS",
    "BlockerSeverity",
    "DimensionScore",
    "FitScore",
    "MatchedEvidence",
    "MissingRequirement",
    "Recommendation",
    "ScoreDimension",
]
