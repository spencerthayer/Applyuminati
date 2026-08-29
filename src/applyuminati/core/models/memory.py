"""Memory: explicit categories, not prompt transcripts.

Storing raw conversations would make the system unauditable and unlearnable.
Instead every durable lesson is a :class:`MemoryRecord` in exactly one
:class:`MemoryKind`, with a retrieval key, a confidence that moves with
observations, provenance, and a supersession pointer so learning is
reversible.

Two hard rules:

* Memory never overwrites the claim ledger. A model observing that a user
  "probably" worked somewhere produces a memory record, not a verified fact.
* Outcomes are recorded without asserting causation. A rejection after using
  a phrasing is correlation; :class:`OutcomeRecord` says so explicitly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.provenance import AssertionLevel, Confidence, Provenance


class MemoryKind(StrEnum):
    """The nine memory categories. Each has a distinct retrieval contract."""

    #: Durable facts about the user's career. Mirrors the claim ledger.
    FACTUAL_CAREER = "factual_career"
    #: How the user writes: accepted phrasing, rejected phrasing, structure.
    WRITING = "writing"
    #: Reusable answers to employer questions, with approval history.
    APPLICATION_ANSWER = "application_answer"
    #: What we know about an employer, with a freshness horizon.
    COMPANY = "company"
    #: How a job source behaves: pagination quirks, rate limits, drift history.
    JOB_SOURCE = "job_source"
    #: Procedures that worked: the click path that completed a Greenhouse form.
    WORKFLOW = "workflow"
    #: What broke, how it was classified, and what recovery was attempted.
    FAILURE_RECOVERY = "failure_recovery"
    #: Application results. Correlational only.
    OUTCOME = "outcome"
    #: Explicit and inferred user preferences about companies, roles, scores.
    USER_PREFERENCE = "user_preference"


class MemoryRecord(BaseModel):
    """One durable lesson.

    ``scope`` + ``key`` is the retrieval address. ``scope`` is the entity the
    memory is about (``company:stripe``, ``source:greenhouse``, ``*``), and
    ``key`` names the lesson within that scope.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    kind: MemoryKind
    scope: str = "*"
    key: str
    #: Human-readable statement of the lesson.
    content: str
    #: Structured payload the consumer understands (selectors, weights, …).
    data: dict[str, Any] = Field(default_factory=dict)
    level: AssertionLevel = AssertionLevel.INFERRED
    provenance: list[Provenance] = Field(default_factory=list)

    #: Times this lesson was reinforced and contradicted.
    supporting_observations: int = 1
    contradicting_observations: int = 0

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None
    #: When set, the memory stops being retrieved after this instant.
    expires_at: datetime | None = None
    #: Points at the record that replaced this one. Nothing is hard-deleted.
    superseded_by: str | None = None

    @property
    def confidence(self) -> Confidence:
        """Laplace-smoothed support ratio."""
        total = self.supporting_observations + self.contradicting_observations
        return (self.supporting_observations + 1) / (total + 2)

    def is_active(self, *, now: datetime | None = None) -> bool:
        if self.superseded_by is not None:
            return False
        if self.expires_at is None:
            return True
        return (now or utcnow()) < self.expires_at


class EditKind(StrEnum):
    """What a user changed about generated material."""

    WORDING = "wording"
    ORDERING = "ordering"
    INCLUSION = "inclusion"
    OMISSION = "omission"
    FACT_CORRECTION = "fact_correction"
    TONE = "tone"
    LENGTH = "length"


class LearningSignal(BaseModel):
    """The difference between what we generated and what the user shipped.

    This is the highest-value training signal in the system, so it is captured
    as structured data — before/after text plus a classification — rather than
    as a free-text note.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: What was edited: ``resume``, ``cover_letter``, ``answer``, ``story``.
    artifact_kind: str
    artifact_id: str | None = None
    #: Path within the artifact, e.g. ``work[0].highlights[2]``.
    target_path: str | None = None
    generated_text: str
    user_text: str
    edit_kinds: list[EditKind] = Field(default_factory=list)
    #: Which job/application the edit happened in, for context-sensitive recall.
    job_id: str | None = None
    application_id: str | None = None
    #: Prompt and model that produced the original, so regressions are traceable.
    prompt_version: str | None = None
    llm_model: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    #: Memory records derived from this signal.
    derived_memory_ids: list[str] = Field(default_factory=list)

    @property
    def is_pure_deletion(self) -> bool:
        return bool(self.generated_text.strip()) and not self.user_text.strip()


class ApprovalSignal(BaseModel):
    """A user accepting or rejecting something wholesale."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: ``wording`` | ``company`` | ``role`` | ``score`` | ``recommendation``
    subject_kind: str
    subject_key: str
    approved: bool
    reason: str | None = None
    job_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class OutcomeRecord(BaseModel):
    """What happened to an application.

    Deliberately free of causal claims. The fields describe *what we did* and
    *what happened*; any inference about why is a separate, weaker memory
    record produced by an explicit analysis step.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    application_id: str
    job_id: str
    #: Terminal or notable state reached, e.g. ``rejected``, ``interview``.
    outcome: str
    occurred_at: datetime = Field(default_factory=utcnow)
    days_to_outcome: float | None = None
    #: Snapshot of inputs at submission time, for later cohort analysis.
    fit_score: float | None = None
    ats: str | None = None
    source: str | None = None
    resume_variant_id: str | None = None
    #: Explicitly acknowledges that we do not know why this happened.
    causation_known: bool = False
    notes: str | None = None


__all__ = [
    "ApprovalSignal",
    "EditKind",
    "LearningSignal",
    "MemoryKind",
    "MemoryRecord",
    "OutcomeRecord",
]
