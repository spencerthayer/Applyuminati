"""Employer application questions and grounded answers.

Two things make this a domain concern rather than a prompt detail:

1. **Sensitivity classification.** Work authorisation, salary, demographics,
   clearances and legal attestations are not ordinary free-text questions.
   They are classified here, and the strategy can require human review for
   anything above :data:`REVIEW_REQUIRED_CLASSES` even in autonomous mode.
2. **Groundedness.** An answer must cite the claims or preferences that
   justify it. A question that cannot be answered from evidence produces an
   :class:`AnswerDraft` with ``status=NEEDS_USER`` — never a plausible guess.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.provenance import Confidence


class QuestionKind(StrEnum):
    """Widget type, which determines how an answer is applied to the form."""

    SHORT_TEXT = "short_text"
    LONG_TEXT = "long_text"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    BOOLEAN = "boolean"
    NUMBER = "number"
    DATE = "date"
    FILE_UPLOAD = "file_upload"
    URL = "url"
    UNKNOWN = "unknown"


class SensitivityClass(StrEnum):
    """How consequential a wrong or invented answer would be."""

    #: Ordinary free-form or factual question.
    ORDINARY = "ordinary"
    #: Legally significant statements about the right to work.
    WORK_AUTHORIZATION = "work_authorization"
    #: Salary expectations and current compensation.
    COMPENSATION = "compensation"
    #: Voluntary EEO self-identification.
    DEMOGRAPHIC = "demographic"
    #: Disability self-identification.
    DISABILITY = "disability"
    #: Veteran status self-identification.
    VETERAN = "veteran"
    #: Security clearances.
    CLEARANCE = "clearance"
    #: Criminal history, credit, drug testing, references.
    BACKGROUND = "background"
    #: "I certify that…", terms acceptance, e-signature.
    LEGAL_ATTESTATION = "legal_attestation"


#: Classes that always stop for a human when the strategy asks for it, and are
#: never answered from a model's inference.
REVIEW_REQUIRED_CLASSES: frozenset[SensitivityClass] = frozenset(
    {
        SensitivityClass.WORK_AUTHORIZATION,
        SensitivityClass.COMPENSATION,
        SensitivityClass.DEMOGRAPHIC,
        SensitivityClass.DISABILITY,
        SensitivityClass.VETERAN,
        SensitivityClass.CLEARANCE,
        SensitivityClass.BACKGROUND,
        SensitivityClass.LEGAL_ATTESTATION,
    }
)

_CLASSIFIERS: tuple[tuple[re.Pattern[str], SensitivityClass], ...] = (
    (
        re.compile(
            r"sponsor|visa|work authoriz|authorized to work|right to work|h-?1b|opt\b|ead\b", re.I
        ),
        SensitivityClass.WORK_AUTHORIZATION,
    ),
    (
        re.compile(r"salary|compensation|pay (range|expectation)|desired (pay|rate)|hourly", re.I),
        SensitivityClass.COMPENSATION,
    ),
    (re.compile(r"disability|adaan|section 503", re.I), SensitivityClass.DISABILITY),
    (re.compile(r"veteran|protected veteran|military service", re.I), SensitivityClass.VETERAN),
    (
        re.compile(r"gender|race|ethnicity|hispanic|latino|self-?identif", re.I),
        SensitivityClass.DEMOGRAPHIC,
    ),
    (re.compile(r"clearance|ts/sci|secret\b|polygraph", re.I), SensitivityClass.CLEARANCE),
    (
        re.compile(r"convict|felony|criminal|background check|drug (test|screen)|credit check", re.I),
        SensitivityClass.BACKGROUND,
    ),
    (
        re.compile(r"certify|i agree|terms and conditions|electronic signature|attest", re.I),
        SensitivityClass.LEGAL_ATTESTATION,
    ),
)


def classify_sensitivity(question_text: str) -> SensitivityClass:
    """Classify a question by its text.

    Deliberately keyword-driven and conservative: a false positive costs one
    review prompt, a false negative costs an unreviewed legal attestation.
    """
    for pattern, sensitivity in _CLASSIFIERS:
        if pattern.search(question_text):
            return sensitivity
    return SensitivityClass.ORDINARY


def normalize_question_key(question_text: str) -> str:
    """Fold a question into a reusable lookup key."""
    lowered = re.sub(r"[^a-z0-9 ]+", " ", question_text.lower())
    tokens = [t for t in lowered.split() if t not in {"the", "a", "an", "your", "you", "do", "are"}]
    return "_".join(tokens[:10])


class ApplicationQuestion(BaseModel):
    """A question observed on an employer's application form."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: Stable key derived from the text, used to look up previous answers.
    key: str = ""
    text: str
    kind: QuestionKind = QuestionKind.UNKNOWN
    required: bool = False
    #: Options for select-type questions, verbatim.
    options: list[str] = Field(default_factory=list)
    max_length: int | None = None
    sensitivity: SensitivityClass = SensitivityClass.ORDINARY
    #: Backend-specific handle used to fill the control (selector, field id).
    field_locator: str | None = None
    #: Which ATS/job this was seen on, for source memory.
    ats: str | None = None

    def model_post_init(self, _context: object) -> None:
        if not self.key:
            object.__setattr__(self, "key", normalize_question_key(self.text))
        if self.sensitivity is SensitivityClass.ORDINARY:
            object.__setattr__(self, "sensitivity", classify_sensitivity(self.text))


class AnswerStatus(StrEnum):
    #: Grounded in evidence and ready to submit.
    READY = "ready"
    #: Grounded but flagged for review because of sensitivity or low confidence.
    NEEDS_REVIEW = "needs_review"
    #: No truthful answer is derivable. We stop and ask.
    NEEDS_USER = "needs_user"
    #: The user edited or wrote this answer.
    USER_PROVIDED = "user_provided"


class AnswerDraft(BaseModel):
    """A proposed answer plus the evidence that justifies it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    question_key: str
    question_text: str
    answer: str | None = None
    status: AnswerStatus = AnswerStatus.NEEDS_USER
    confidence: Confidence = 0.0
    #: Claim ids from the career profile backing this answer.
    evidence_claim_ids: list[str] = Field(default_factory=list)
    #: Ids of previously approved answers reused here.
    reused_answer_ids: list[str] = Field(default_factory=list)
    #: Why the answer is what it is, in one line.
    rationale: str = ""
    sensitivity: SensitivityClass = SensitivityClass.ORDINARY
    llm_model: str | None = None
    prompt_version: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_submittable(self) -> bool:
        return self.status in (AnswerStatus.READY, AnswerStatus.USER_PROVIDED) and bool(
            self.answer
        )

    @property
    def requires_review(self) -> bool:
        return (
            self.status is AnswerStatus.NEEDS_REVIEW
            or self.sensitivity in REVIEW_REQUIRED_CLASSES
        )


__all__ = [
    "REVIEW_REQUIRED_CLASSES",
    "AnswerDraft",
    "AnswerStatus",
    "ApplicationQuestion",
    "QuestionKind",
    "SensitivityClass",
    "classify_sensitivity",
    "normalize_question_key",
]
