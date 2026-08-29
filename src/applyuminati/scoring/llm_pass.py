"""Optional LLM enrichment of the deterministic score.

The model may ONLY nudge existing dimension scores within a bounded range, add
uncertainties, add missing requirements, and rewrite the explanation. It may
NOT set the overall score, invent dimensions, remove hard blockers, or change
the recommendation — the engine recomputes all of those from the adjusted
dimensions, so the LLM's output is structurally unprivileged.

If the call fails, the input score is returned unchanged with one added
uncertainty line: the run never fails because an enrichment was unavailable.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from applyuminati.core.errors import ApplyuminatiError
from applyuminati.core.logging import get_logger
from applyuminati.core.models.job import Job
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.scoring import (
    BlockerSeverity,
    DimensionScore,
    FitScore,
    MissingRequirement,
    Recommendation,
    ScoreDimension,
)
from applyuminati.scoring.engine import SCORER_VERSION, _confidence, _explain, _weighted_overall


class _StructuredCompletionClient(Protocol):
    """The one `LLMClient` capability this module needs.

    Declared locally, rather than importing `applyuminati.llm.client.LLMClient`,
    because `scoring` and `llm` are independent siblings in the layered
    architecture: scoring must stay usable (deterministically) with no LLM
    dependency at all, and this Protocol is satisfied structurally by the
    real client without either module importing the other.
    """

    async def structured(
        self, prompt_id: str, *, schema: type[BaseModel], **kwargs: Any
    ) -> tuple[Any, Any]: ...


log = get_logger(__name__)

#: Maximum magnitude of a model-driven dimension adjustment. Clamped in
#: Python so a misbehaving prompt can never move a score more than this.
MAX_ADJUSTMENT = 0.2
ENRICH_VERSION = "score.enrich/2026-01"
_PROMPT_ID = "score.enrich"


# -- local schema --------------------------------------------------------
# Defined here (not imported from llm.prompts.scoring) so this module does
# not depend on the llm package. The prompt task registers a schema with the
# same field names under prompt id "score.enrich".


class _DimensionAdjustment(BaseModel):
    dimension: str
    delta: float
    rationale: str = ""


class _MissingRequirementSuggestion(BaseModel):
    requirement: str
    severity: str = "significant"
    note: str | None = None


class _EnrichmentSchema(BaseModel):
    adjustments: list[_DimensionAdjustment] = Field(default_factory=list)
    missing_requirements: list[_MissingRequirementSuggestion] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    explanation: str = ""


class ScoreEnrichmentResult:  # pragma: no cover - placeholder for the schema the LLM task owns
    """The structured response the ``score.enrich`` prompt produces.

    The real schema lives in ``applyuminati.llm.prompts.scoring``; the fields
    used here are documented so this module does not import the prompts
    package (which would invert the layering: scoring must not depend on llm).
    """


def _clamp_delta(delta: float) -> float:
    return max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, float(delta)))


def _apply_adjustments(
    score: FitScore, adjustments: list[_DimensionAdjustment]
) -> list[DimensionScore]:
    by_name = {dimension.dimension: dimension for dimension in score.dimensions}
    for adjustment in adjustments:
        try:
            dimension_enum = ScoreDimension(adjustment.dimension)
        except ValueError:
            log.warning("score.unknown_dimension_adjustment", dimension=adjustment.dimension)
            continue
        dimension = by_name.get(dimension_enum)
        if dimension is None:
            continue
        delta = _clamp_delta(adjustment.delta)
        adjusted_score = max(0.0, min(1.0, dimension.score + delta))
        by_name[dimension_enum] = dimension.model_copy(
            update={"score": adjusted_score, "llm_adjusted": True}
        )
    return list(by_name.values())


def _merge_missing(
    existing: list[MissingRequirement], additions: list[_MissingRequirementSuggestion]
) -> list[MissingRequirement]:
    present = {item.requirement.lower() for item in existing}
    merged = list(existing)
    for addition in additions:
        requirement = addition.requirement.strip()
        if not requirement or requirement.lower() in present:
            continue
        try:
            severity = BlockerSeverity(addition.severity.lower())
        except ValueError:
            severity = BlockerSeverity.SIGNIFICANT
        merged.append(
            MissingRequirement(
                requirement=requirement,
                severity=severity,
                note=addition.note,
                partially_evidenced=False,
            )
        )
    return merged


def _recompute(score: FitScore, dimensions: list[DimensionScore]) -> FitScore:
    """Re-derive overall/confidence/recommendation from adjusted dimensions."""
    from applyuminati.core.models.scoring import BlockerSeverity as _BS

    baseline = _weighted_overall(dimensions)
    confidence = _confidence(dimensions)
    has_hard = any(m.severity is _BS.HARD for m in score.missing_requirements)
    if has_hard:
        overall = min(baseline, 0.25)
        recommendation = Recommendation.SKIP
    elif baseline < 0.3:  # strategy threshold lives on the caller's score; reuse skip_below
        overall = baseline
        recommendation = Recommendation.SKIP
    elif baseline >= 0.55 and confidence >= 0.45:
        overall = baseline
        recommendation = Recommendation.APPLY
    else:
        overall = baseline
        recommendation = Recommendation.INVESTIGATE
    return score.model_copy(
        update={
            "dimensions": dimensions,
            "overall": overall,
            "confidence": confidence,
            "recommendation": recommendation,
            "explanation": _explain(dimensions),
            "scorer_version": f"{SCORER_VERSION}+enrich",
        }
    )


async def enrich_score(
    score: FitScore, job: Job, profile: CareerProfile, client: _StructuredCompletionClient
) -> FitScore:
    """Run the optional LLM pass and return an enriched (or unchanged) score."""
    try:
        result, response = await client.structured(
            _PROMPT_ID,
            schema=_EnrichmentSchema,
            job_title=job.title,
            company=job.company,
            description_excerpt=(job.description or "")[:2000],
            profile_summary=_summarise(profile),
            dimensions=[
                {"dimension": d.dimension.value, "score": d.score, "rationale": d.rationale}
                for d in score.dimensions
            ],
        )
    except ApplyuminatiError as exc:
        log.warning("score.enrich_failed", job_id=job.id, error=exc.code)
        return score.model_copy(
            update={
                "uncertainties": [
                    *score.uncertainties,
                    f"LLM enrichment unavailable: {exc.message}",
                ],
            }
        )
    except Exception as exc:
        log.warning("score.enrich_unexpected_error", job_id=job.id, error=str(exc))
        return score.model_copy(
            update={
                "uncertainties": [*score.uncertainties, "LLM enrichment skipped due to an error"],
            }
        )

    dimensions = _apply_adjustments(score, list(result.adjustments))
    missing = _merge_missing(score.missing_requirements, list(result.missing_requirements))
    uncertainties = list(dict.fromkeys([*score.uncertainties, *result.uncertainties]))
    enriched = score.model_copy(
        update={
            "missing_requirements": missing,
            "uncertainties": uncertainties,
            "llm_provider": response.provider,
            "llm_model": response.model,
            "llm_prompt_version": ENRICH_VERSION,
        }
    )
    return _recompute(enriched, dimensions)


def _summarise(profile: CareerProfile) -> str:
    titles = ", ".join(profile.targets.titles[:3]) or "(unspecified)"
    skills = ", ".join(sorted(profile.skill_names())[:15]) or "(unspecified)"
    return f"Target titles: {titles}. Skills: {skills}."
