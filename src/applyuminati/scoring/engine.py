"""The deterministic scoring engine.

Composes the dimension functions, applies weights, and enforces the one rule
that keeps the score honest: **a hard blocker caps the overall score and
forces SKIP, regardless of how well everything else matched.** No LLM is
involved here, so this is the floor that always runs even with zero providers
configured.
"""

from __future__ import annotations

from applyuminati.core.models.job import Job
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.scoring import (
    DEFAULT_WEIGHTS,
    BlockerSeverity,
    DimensionScore,
    FitScore,
    Recommendation,
    ScoreDimension,
)
from applyuminati.core.strategy import SearchStrategy
from applyuminati.scoring.dimensions import score_dimensions

__all__ = ["SCORER_VERSION", "score_job"]

SCORER_VERSION = "baseline/1"
_HARD_BLOCKER_CAP = 0.25


def _weighted_overall(dimensions: list[DimensionScore]) -> float:
    total_weight = sum(dimension.weight for dimension in dimensions if dimension.weight > 0)
    if total_weight <= 0:
        return 0.5
    weighted = sum(dimension.weighted for dimension in dimensions)
    return min(1.0, weighted / total_weight)


def _confidence(dimensions: list[DimensionScore]) -> float:
    total_weight = sum(dimension.weight for dimension in dimensions if dimension.weight > 0)
    if total_weight <= 0:
        return 0.3
    weighted = sum(dimension.confidence * dimension.weight for dimension in dimensions)
    return min(1.0, weighted / total_weight)


def _explain(dimensions: list[DimensionScore]) -> str:
    sorted_dims = sorted(
        (d for d in dimensions if d.weight > 0), key=lambda d: d.weighted, reverse=True
    )
    top = sorted_dims[:2]
    weak = sorted((d for d in dimensions if d.weight > 0), key=lambda d: d.score)[:1]
    parts = [f"Strongest: {d.dimension.value} ({d.rationale})." for d in top]
    if weak:
        parts.append(f"Weakest: {weak[0].dimension.value} ({weak[0].rationale}).")
    return " ".join(parts)


def score_job(
    job: Job,
    profile: CareerProfile,
    strategy: SearchStrategy,
    *,
    weights: dict[ScoreDimension, float] | None = None,
) -> FitScore:
    """Produce a complete, inspectable :class:`FitScore` deterministically."""
    weight_map = weights or DEFAULT_WEIGHTS
    dimensions, evidence, missing, uncertainties = score_dimensions(job, profile, strategy)

    # Apply the configured weights to the dimension scores.
    weighted: list[DimensionScore] = []
    for dimension in dimensions:
        weighted.append(
            dimension.model_copy(update={"weight": weight_map.get(dimension.dimension, 0.0)})
        )

    baseline = _weighted_overall(weighted)
    confidence = _confidence(weighted)
    has_hard_blocker = any(m.severity is BlockerSeverity.HARD for m in missing)

    if has_hard_blocker:
        overall = min(baseline, _HARD_BLOCKER_CAP)
        recommendation = Recommendation.SKIP
    elif baseline < strategy.skip_below_score:
        overall = baseline
        recommendation = Recommendation.SKIP
    elif (
        baseline >= strategy.minimum_fit_score
        and confidence >= strategy.minimum_evidence_confidence
    ):
        overall = baseline
        recommendation = Recommendation.APPLY
    else:
        overall = baseline
        recommendation = Recommendation.INVESTIGATE

    return FitScore(
        job_id=job.id,
        profile_id=profile.id,
        overall=overall,
        confidence=confidence,
        recommendation=recommendation,
        dimensions=weighted,
        matched_evidence=evidence,
        missing_requirements=missing,
        uncertainties=uncertainties,
        explanation=_explain(weighted),
        baseline_overall=baseline,
        scorer_version=SCORER_VERSION,
    )
