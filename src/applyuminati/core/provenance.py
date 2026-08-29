"""Provenance and epistemic status.

Applyuminati's central invariant: **models do not own canonical truth**
(architectural rule 5). Every statement the system holds about a user, a
company or a job carries an :class:`AssertionLevel` describing *how we know
it*, and a :class:`Provenance` record describing *where it came from*.

The levels are ordered by epistemic strength. Promotion up the ladder is a
deliberate, recorded act — :func:`can_auto_promote` refuses every promotion
that would turn a model's guess into a fact without a human in the loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid


class AssertionLevel(StrEnum):
    """How a claim is known. Ordered weakest to strongest by :data:`LEVEL_RANK`."""

    #: A model proposed this. Never used as a fact; requires review.
    MODEL_SUGGESTION = "model_suggestion"
    #: Derived by rule or model from other data (e.g. "~8 years Python").
    INFERRED = "inferred"
    #: Collected from a third party about a company/role. Ages out.
    EXTERNAL_RESEARCH = "external_research"
    #: Wording produced for a document. Says nothing about truth on its own;
    #: the *claims inside it* must each trace to stronger evidence.
    GENERATED_WORDING = "generated_wording"
    #: A user's stated preference. True by definition, but not a fact about the world.
    PREFERENCE = "preference"
    #: An interpretation a model offered and the user explicitly accepted.
    USER_APPROVED = "user_approved"
    #: Asserted by the user as fact, or imported from their own resume/artifacts.
    VERIFIED = "verified"


LEVEL_RANK: dict[AssertionLevel, int] = {
    AssertionLevel.MODEL_SUGGESTION: 0,
    AssertionLevel.INFERRED: 1,
    AssertionLevel.EXTERNAL_RESEARCH: 2,
    AssertionLevel.GENERATED_WORDING: 3,
    AssertionLevel.PREFERENCE: 4,
    AssertionLevel.USER_APPROVED: 5,
    AssertionLevel.VERIFIED: 6,
}

#: Levels that may be quoted as fact in an application or resume.
FACTUAL_LEVELS: frozenset[AssertionLevel] = frozenset(
    {AssertionLevel.VERIFIED, AssertionLevel.USER_APPROVED}
)

#: The only promotions a machine may perform unattended: strictly none that
#: cross into factual territory.
_AUTO_PROMOTABLE: frozenset[tuple[AssertionLevel, AssertionLevel]] = frozenset(
    {
        (AssertionLevel.MODEL_SUGGESTION, AssertionLevel.INFERRED),
        (AssertionLevel.MODEL_SUGGESTION, AssertionLevel.EXTERNAL_RESEARCH),
        (AssertionLevel.INFERRED, AssertionLevel.EXTERNAL_RESEARCH),
    }
)


def can_auto_promote(current: AssertionLevel, target: AssertionLevel) -> bool:
    """Return ``True`` only if a machine may make this transition unattended.

    Any transition into :data:`FACTUAL_LEVELS` returns ``False``: an LLM must
    never silently promote an inference into a verified fact.
    """
    if target == current:
        return True
    if target in FACTUAL_LEVELS:
        return False
    if LEVEL_RANK[target] < LEVEL_RANK[current]:
        # Demotion (e.g. research went stale) is always permitted.
        return True
    return (current, target) in _AUTO_PROMOTABLE


class ProvenanceKind(StrEnum):
    """The class of origin for a claim."""

    USER_INPUT = "user_input"
    RESUME_IMPORT = "resume_import"
    PORTFOLIO_ARTIFACT = "portfolio_artifact"
    JOB_SOURCE = "job_source"
    COMPANY_RESEARCH = "company_research"
    EMAIL = "email"
    BROWSER_OBSERVATION = "browser_observation"
    LLM = "llm"
    AGENT_BACKEND = "agent_backend"
    DERIVED = "derived"
    SYSTEM = "system"


Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class Provenance(BaseModel):
    """Where a single claim came from, and when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProvenanceKind
    #: Stable identifier of the origin: plugin slug, model id, file path, URL.
    origin: str
    #: Optional pointer into the origin (field path, message id, DOM selector).
    locator: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    #: How much the *origin* is trusted, independent of the assertion level.
    confidence: Confidence = 1.0
    #: How long this observation stays current. ``None`` means "does not age".
    ttl_seconds: int | None = None
    notes: str | None = None

    def is_stale(self, *, now: datetime | None = None) -> bool:
        """Return ``True`` when the observation has outlived its TTL.

        Stale external research must not be presented as current
        (see :mod:`applyuminati.core.models.research`).
        """
        if self.ttl_seconds is None:
            return False
        reference = now or utcnow()
        return reference - self.observed_at > timedelta(seconds=self.ttl_seconds)

    def age_seconds(self, *, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.observed_at).total_seconds()


class Claim(BaseModel):
    """A single statement plus its epistemic status.

    ``Claim`` is the unit that resume tailoring, questionnaire answering and
    company research all traffic in. Generated content links back to the claim
    ids that justify it (see ``EvidenceLink``), which is what makes
    "every factual career claim is traceable" enforceable rather than
    aspirational.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: Free-text statement, e.g. "Led migration of billing to Kafka".
    statement: str
    level: AssertionLevel
    provenance: list[Provenance] = Field(default_factory=list)
    #: Optional machine-readable payload (metric value, date range, URL...).
    data: dict[str, Any] = Field(default_factory=dict)
    #: Tags used for retrieval: skill names, employer slug, story theme.
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    #: Set when a stronger claim replaced this one. Nothing is hard-deleted, so
    #: learning remains reversible.
    superseded_by: str | None = None

    @model_validator(mode="after")
    def _require_provenance_for_facts(self) -> Self:
        if self.level in FACTUAL_LEVELS and not self.provenance:
            msg = f"claim {self.id} is {self.level} but carries no provenance"
            raise ValueError(msg)
        return self

    @property
    def is_factual(self) -> bool:
        """True when the claim may be quoted as fact in generated material."""
        return self.level in FACTUAL_LEVELS and self.superseded_by is None

    def promote(
        self,
        target: AssertionLevel,
        *,
        by_user: bool,
        provenance: Provenance | None = None,
    ) -> Claim:
        """Return a copy at ``target``.

        Raises ``ValueError`` when a machine attempts a promotion reserved for
        a human. ``by_user=True`` is only ever passed from an explicit user
        action handler, never from a model-driven code path.
        """
        if not by_user and not can_auto_promote(self.level, target):
            msg = (
                f"refusing to auto-promote claim {self.id} from {self.level} "
                f"to {target}; this transition requires explicit user approval"
            )
            raise ValueError(msg)
        return self.model_copy(
            update={
                "level": target,
                "updated_at": utcnow(),
                "provenance": [*self.provenance, provenance] if provenance else self.provenance,
            }
        )


class EvidenceLink(BaseModel):
    """A pointer from generated text back to the claim that justifies it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    #: Where in the generated artifact this evidence is used
    #: (e.g. ``work[1].highlights[0]`` for a JSON Resume path).
    target_path: str
    #: Verbatim fragment of the generated text this link covers.
    excerpt: str | None = None
    #: 0..1 strength of the association, for review UIs.
    strength: Confidence = 1.0


__all__ = [
    "FACTUAL_LEVELS",
    "LEVEL_RANK",
    "AssertionLevel",
    "Claim",
    "Confidence",
    "EvidenceLink",
    "Provenance",
    "ProvenanceKind",
    "can_auto_promote",
]
