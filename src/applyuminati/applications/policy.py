"""Questionnaire answer-authority policy.

Decides whether a question may be answered automatically, declined, or must
stop for a human. Provenance is retained on every draft. A generated
suggestion is never treated as a verified career fact: the assertion level
travels with the answer and :func:`can_auto_promote` still refuses any
promotion into factual territory.
"""

from __future__ import annotations

from typing import Never

from applyuminati.core.models.profile import CareerProfile, QuestionnaireDefault
from applyuminati.core.models.questionnaire import (
    AnswerAuthority,
    AnswerDraft,
    AnswerStatus,
    ApplicationQuestion,
    SensitivityClass,
)
from applyuminati.core.provenance import (
    FACTUAL_LEVELS,
    AssertionLevel,
    Claim,
)

__all__ = [
    "DEFAULT_AUTHORITY",
    "DECLINED_OPTIONAL",
    "PolicyDecision",
    "QuestionnairePolicy",
    "authority_for",
]

DECLINED_OPTIONAL = "[declined]"

DEFAULT_AUTHORITY: dict[SensitivityClass, AnswerAuthority] = {
    SensitivityClass.ORDINARY: AnswerAuthority.ALWAYS_ANSWER,
    SensitivityClass.WORK_AUTHORIZATION: AnswerAuthority.ANSWER_IF_VERIFIED,
    SensitivityClass.COMPENSATION: AnswerAuthority.REQUIRE_REVIEW,
    SensitivityClass.DEMOGRAPHIC: AnswerAuthority.DECLINE_IF_OPTIONAL,
    SensitivityClass.DISABILITY: AnswerAuthority.DECLINE_IF_OPTIONAL,
    SensitivityClass.VETERAN: AnswerAuthority.DECLINE_IF_OPTIONAL,
    SensitivityClass.CLEARANCE: AnswerAuthority.REQUIRE_REVIEW,
    SensitivityClass.BACKGROUND: AnswerAuthority.REQUIRE_REVIEW,
    SensitivityClass.LEGAL_ATTESTATION: AnswerAuthority.REQUIRE_REVIEW,
}


class PolicyDecision:
    """One policy application. The draft is the answer; this is why."""

    __slots__ = ("authority", "draft", "paused")

    def __init__(self, draft: AnswerDraft, authority: AnswerAuthority, *, paused: bool) -> None:
        self.draft = draft
        self.authority = authority
        self.paused = paused


def authority_for(
    sensitivity: SensitivityClass,
    *,
    overrides: dict[SensitivityClass, AnswerAuthority] | None = None,
) -> AnswerAuthority:
    if overrides and sensitivity in overrides:
        return overrides[sensitivity]
    return DEFAULT_AUTHORITY[sensitivity]


class QuestionnairePolicy:
    """Apply authority rules to one question against a career profile."""

    def __init__(
        self,
        profile: CareerProfile,
        *,
        overrides: dict[SensitivityClass, AnswerAuthority] | None = None,
    ) -> None:
        self.profile = profile
        self.overrides = overrides or {}

    def decide(self, question: ApplicationQuestion) -> PolicyDecision:
        authority = authority_for(question.sensitivity, overrides=self.overrides)
        default = self._default_for(question)
        claim = self._claim_for(question, default)

        if authority is AnswerAuthority.REQUIRE_REVIEW:
            return self._review(question, default, claim, authority, "policy requires review")
        if authority is AnswerAuthority.DECLINE_IF_OPTIONAL:
            if not question.required:
                return PolicyDecision(
                    AnswerDraft(
                        question_key=question.key,
                        question_text=question.text,
                        answer=DECLINED_OPTIONAL,
                        status=AnswerStatus.READY,
                        sensitivity=question.sensitivity,
                        rationale="optional question declined by policy",
                    ),
                    authority,
                    paused=False,
                )
            return self._review(
                question, default, claim, authority, "required question cannot be declined"
            )
        if authority is AnswerAuthority.ANSWER_IF_VERIFIED:
            if claim is not None and claim.level in FACTUAL_LEVELS and default is not None:
                return self._ready(question, default, claim, authority, "verified profile fact")
            return self._review(
                question, default, claim, authority, "no verified fact for this question"
            )
        if authority is AnswerAuthority.REUSE_APPROVED:
            if default is not None and default.level in (
                AssertionLevel.USER_APPROVED,
                AssertionLevel.VERIFIED,
                AssertionLevel.PREFERENCE,
            ):
                return self._ready(question, default, claim, authority, "reused approved answer")
            return self._review(
                question, default, claim, authority, "no previously approved answer"
            )
        if authority is AnswerAuthority.ALWAYS_ANSWER:
            if default is not None:
                # Preference and verified facts may be used. A model suggestion
                # may be drafted but stays NEEDS_REVIEW so it cannot become truth.
                if default.level is AssertionLevel.MODEL_SUGGESTION:
                    return self._review(
                        question,
                        default,
                        claim,
                        authority,
                        "generated suggestion cannot be submitted as fact",
                    )
                return self._ready(question, default, claim, authority, "profile answer available")
            return self._review(question, None, claim, authority, "no profile answer available")
        if authority is AnswerAuthority.NEVER_ANSWER:
            return PolicyDecision(
                AnswerDraft(
                    question_key=question.key,
                    question_text=question.text,
                    answer=None,
                    status=AnswerStatus.NEEDS_USER,
                    sensitivity=question.sensitivity,
                    rationale="policy forbids answering this question",
                ),
                authority,
                paused=True,
            )
        never_authority: Never = authority
        raise AssertionError(f"unhandled answer authority: {never_authority}")

    def _default_for(self, question: ApplicationQuestion) -> QuestionnaireDefault | None:
        return next(
            (item for item in self.profile.questionnaire_defaults if item.key == question.key),
            None,
        )

    def _claim_for(
        self, question: ApplicationQuestion, default: QuestionnaireDefault | None
    ) -> Claim | None:
        claim_ids = list(default.claim_ids) if default is not None else []
        if not claim_ids:
            return None
        return next((claim for claim in self.profile.claims if claim.id in claim_ids), None)

    def _ready(
        self,
        question: ApplicationQuestion,
        default: QuestionnaireDefault,
        _claim: Claim | None,
        authority: AnswerAuthority,
        rationale: str,
    ) -> PolicyDecision:
        return PolicyDecision(
            AnswerDraft(
                question_key=question.key,
                question_text=question.text,
                answer=default.answer,
                status=AnswerStatus.READY,
                sensitivity=question.sensitivity,
                evidence_claim_ids=list(default.claim_ids),
                reused_answer_ids=[default.id],
                rationale=rationale,
                confidence=default.confidence,
            ),
            authority,
            paused=False,
        )

    def _review(
        self,
        question: ApplicationQuestion,
        default: QuestionnaireDefault | None,
        _claim: Claim | None,
        authority: AnswerAuthority,
        rationale: str,
    ) -> PolicyDecision:
        answer = default.answer if default is not None else None
        status = AnswerStatus.NEEDS_REVIEW if answer else AnswerStatus.NEEDS_USER
        return PolicyDecision(
            AnswerDraft(
                question_key=question.key,
                question_text=question.text,
                answer=answer,
                status=status,
                sensitivity=question.sensitivity,
                evidence_claim_ids=list(default.claim_ids) if default is not None else [],
                reused_answer_ids=[default.id] if default is not None else [],
                rationale=rationale,
            ),
            authority,
            paused=True,
        )
