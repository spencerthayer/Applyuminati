"""Questionnaire answer-authority policy."""

from __future__ import annotations

from applyuminati.applications.policy import (
    DECLINED_OPTIONAL,
    QuestionnairePolicy,
)
from applyuminati.core.models.profile import CareerProfile, QuestionnaireDefault
from applyuminati.core.models.questionnaire import (
    AnswerAuthority,
    AnswerStatus,
    ApplicationQuestion,
    QuestionKind,
    SensitivityClass,
)
from applyuminati.core.provenance import AssertionLevel, Claim, Provenance, ProvenanceKind


def _profile(*defaults: QuestionnaireDefault, claims: list[Claim] | None = None) -> CareerProfile:
    return CareerProfile(questionnaire_defaults=list(defaults), claims=list(claims or []))


def test_verified_work_authorization_is_answered() -> None:
    claim = Claim(
        statement="Authorized to work in the US",
        level=AssertionLevel.VERIFIED,
        provenance=[Provenance(kind=ProvenanceKind.USER_INPUT, origin="profile")],
    )
    default = QuestionnaireDefault(
        key="legally_authorized_to_work_in_united_states",
        question_text="Are you legally authorized to work in the United States?",
        answer="Yes",
        level=AssertionLevel.VERIFIED,
        claim_ids=[claim.id],
    )
    question = ApplicationQuestion(
        text="Are you legally authorized to work in the United States?",
        kind=QuestionKind.BOOLEAN,
        required=True,
    )
    decision = QuestionnairePolicy(_profile(default, claims=[claim])).decide(question)
    assert decision.authority is AnswerAuthority.ANSWER_IF_VERIFIED
    assert decision.paused is False
    assert decision.draft.answer == "Yes"
    assert decision.draft.status is AnswerStatus.READY


def test_optional_demographic_is_declined() -> None:
    question = ApplicationQuestion(
        text="Would you like to voluntarily disclose disability status?",
        kind=QuestionKind.BOOLEAN,
        required=False,
    )
    decision = QuestionnairePolicy(_profile()).decide(question)
    assert decision.authority is AnswerAuthority.DECLINE_IF_OPTIONAL
    assert decision.draft.answer == DECLINED_OPTIONAL
    assert decision.paused is False


def test_legal_attestation_always_stops() -> None:
    question = ApplicationQuestion(
        text="Do you certify that every statement in this application is accurate?",
        kind=QuestionKind.BOOLEAN,
        required=True,
    )
    decision = QuestionnairePolicy(_profile()).decide(question)
    assert decision.authority is AnswerAuthority.REQUIRE_REVIEW
    assert decision.paused is True
    assert decision.draft.status is AnswerStatus.NEEDS_USER


def test_generated_suggestion_does_not_become_a_fact() -> None:
    default = QuestionnaireDefault(
        key="years_of_python_experience",
        question_text="Years of Python experience?",
        answer="12",
        level=AssertionLevel.MODEL_SUGGESTION,
    )
    question = ApplicationQuestion(text="Years of Python experience?", kind=QuestionKind.NUMBER)
    decision = QuestionnairePolicy(_profile(default)).decide(question)
    assert decision.paused is True
    assert decision.draft.status is AnswerStatus.NEEDS_REVIEW
    assert decision.draft.answer == "12"


def test_missing_verified_fact_is_not_fabricated() -> None:
    question = ApplicationQuestion(
        text="Are you legally authorized to work in the United States?",
        required=True,
    )
    decision = QuestionnairePolicy(_profile()).decide(question)
    assert decision.paused is True
    assert decision.draft.answer is None
    assert decision.draft.status is AnswerStatus.NEEDS_USER


def test_never_answer_proposes_no_wording() -> None:
    default = QuestionnaireDefault(
        key="years_of_python_experience",
        question_text="Years of Python experience?",
        answer="12",
        level=AssertionLevel.VERIFIED,
    )
    question = ApplicationQuestion(text="Years of Python experience?", kind=QuestionKind.NUMBER)
    policy = QuestionnairePolicy(
        _profile(default),
        overrides={SensitivityClass.ORDINARY: AnswerAuthority.NEVER_ANSWER},
    )
    decision = policy.decide(question)
    assert decision.paused is True
    assert decision.draft.answer is None
    assert decision.draft.status is AnswerStatus.NEEDS_USER


def test_approved_answer_can_be_reused() -> None:
    default = QuestionnaireDefault(
        key="willing_to_relocate",
        question_text="Willing to relocate?",
        answer="No",
        level=AssertionLevel.USER_APPROVED,
    )
    question = ApplicationQuestion(text="Willing to relocate?", kind=QuestionKind.BOOLEAN)
    policy = QuestionnairePolicy(
        _profile(default),
        overrides={SensitivityClass.ORDINARY: AnswerAuthority.REUSE_APPROVED},
    )
    decision = policy.decide(question)
    assert decision.paused is False
    assert decision.draft.reused_answer_ids == [default.id]
