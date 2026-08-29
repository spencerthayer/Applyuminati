"""Search and application strategy.

The strategy is the user's dial-set: how broadly to search, how strict to be,
how much to apply. It is stored as **exact numeric values**, never as vague
labels — the UI may render a slider labelled "balanced", but what persists is
``application_volume_bias = 0.5``.

Presets exist for convenience only; selecting one materialises concrete
numbers into the stored strategy so later tuning is always visible.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from applyuminati.core.settings import ExecutionMode

#: A normalised 0..1 dial. 0 = one extreme, 1 = the other; each field documents which.
Dial = Annotated[float, Field(ge=0.0, le=1.0)]


class RemotePreference(StrEnum):
    REMOTE_ONLY = "remote_only"
    REMOTE_PREFERRED = "remote_preferred"
    HYBRID_PREFERRED = "hybrid_preferred"
    ONSITE_PREFERRED = "onsite_preferred"
    NO_PREFERENCE = "no_preference"


class Strictness(StrEnum):
    """Applied to compensation, location and seniority independently."""

    #: Violating the constraint is a hard blocker.
    HARD = "hard"
    #: Violating the constraint costs score but does not disqualify.
    SOFT = "soft"
    #: The constraint is recorded but ignored while ranking.
    IGNORED = "ignored"


class SearchStrategy(BaseModel):
    """Tunable search/apply behaviour. Every field is inspectable and exact."""

    model_config = ConfigDict(extra="forbid")

    name: str = "default"

    # -- breadth / effort -------------------------------------------------
    #: 0 = fast and shallow (fewer sources, fewer pages), 1 = slow and thorough.
    depth_bias: Dial = 0.5
    #: 0 = few, highly-matched applications; 1 = high volume.
    application_volume_bias: Dial = 0.35
    #: 0 = only near-exact title matches; 1 = exploratory adjacent titles.
    title_exploration: Dial = 0.3

    # -- constraints ------------------------------------------------------
    compensation_strictness: Strictness = Strictness.SOFT
    location_strictness: Strictness = Strictness.SOFT
    remote_preference: RemotePreference = RemotePreference.REMOTE_PREFERRED
    #: How many levels away from the target seniority is still acceptable.
    seniority_tolerance_levels: int = Field(default=1, ge=0, le=4)
    work_authorization_is_hard_blocker: bool = True

    # -- thresholds -------------------------------------------------------
    #: Below this overall fit score a job is not shortlisted.
    minimum_fit_score: float = Field(default=0.55, ge=0.0, le=1.0)
    #: Below this scoring confidence the recommendation is downgraded to
    #: ``investigate`` rather than ``apply``.
    minimum_evidence_confidence: float = Field(default=0.45, ge=0.0, le=1.0)
    #: A job scoring below this is recommended ``skip`` outright.
    skip_below_score: float = Field(default=0.3, ge=0.0, le=1.0)

    # -- volume limits ----------------------------------------------------
    max_applications_per_run: int = Field(default=5, ge=0)
    max_applications_per_day: int = Field(default=15, ge=0)
    max_jobs_per_source_per_run: int = Field(default=200, ge=1)

    # -- preferences ------------------------------------------------------
    preferred_industries: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)
    preferred_companies: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)

    # -- autonomy ---------------------------------------------------------
    execution_mode: ExecutionMode = ExecutionMode.RESEARCH_ONLY
    #: Even in ``autonomous_submit``, these always stop for a human.
    require_review_for_sensitive_questions: bool = True
    require_review_above_compensation_usd: int | None = None

    @model_validator(mode="after")
    def _validate_thresholds(self) -> Self:
        if self.skip_below_score > self.minimum_fit_score:
            msg = (
                f"skip_below_score ({self.skip_below_score}) must not exceed "
                f"minimum_fit_score ({self.minimum_fit_score})"
            )
            raise ValueError(msg)
        overlap = set(self.preferred_companies) & set(self.excluded_companies)
        if overlap:
            msg = f"companies both preferred and excluded: {sorted(overlap)}"
            raise ValueError(msg)
        return self

    @property
    def pages_per_source(self) -> int:
        """Pagination depth implied by :attr:`depth_bias` (1..10 pages)."""
        return max(1, round(1 + self.depth_bias * 9))


PRESETS: dict[str, SearchStrategy] = {
    "precise": SearchStrategy(
        name="precise",
        depth_bias=0.7,
        application_volume_bias=0.1,
        title_exploration=0.1,
        compensation_strictness=Strictness.HARD,
        location_strictness=Strictness.HARD,
        seniority_tolerance_levels=0,
        minimum_fit_score=0.75,
        minimum_evidence_confidence=0.6,
        skip_below_score=0.45,
        max_applications_per_run=3,
        max_applications_per_day=5,
    ),
    "balanced": SearchStrategy(name="balanced"),
    "wide": SearchStrategy(
        name="wide",
        depth_bias=0.9,
        application_volume_bias=0.8,
        title_exploration=0.7,
        compensation_strictness=Strictness.SOFT,
        location_strictness=Strictness.SOFT,
        seniority_tolerance_levels=2,
        minimum_fit_score=0.4,
        minimum_evidence_confidence=0.3,
        skip_below_score=0.2,
        max_applications_per_run=15,
        max_applications_per_day=40,
        max_jobs_per_source_per_run=500,
    ),
}


def preset(name: str) -> SearchStrategy:
    """Materialise a named preset into concrete numbers."""
    try:
        return PRESETS[name].model_copy(deep=True)
    except KeyError as exc:  # pragma: no cover - guarded at the CLI/API boundary
        msg = f"unknown strategy preset {name!r}; known: {sorted(PRESETS)}"
        raise ValueError(msg) from exc


__all__ = ["PRESETS", "Dial", "RemotePreference", "SearchStrategy", "Strictness", "preset"]
