"""Advance an ApplicationAttempt: policy, handoff, checkpoints, evidence.

Shared by every ApplicationDriver so Greenhouse-shaped logic does not leak
into core. Drivers supply page interpretation; this module applies policy,
records state, and refuses to submit twice.
"""

from __future__ import annotations

from collections.abc import Callable

from applyuminati.applications.driver import DriverContext, DriverOutcome, DriverOutcomeKind
from applyuminati.applications.modes import ActionForbiddenError, check, permitted_actions
from applyuminati.applications.policy import QuestionnairePolicy
from applyuminati.browser.base import (
    HANDOFF_CONDITIONS,
    BrowserSession,
    ControlOwner,
    PageCondition,
    PageElement,
    PageObservation,
)
from applyuminati.core.clock import utcnow
from applyuminati.core.errors import FailureCategory
from applyuminati.core.models.execution import (
    ApplicationAttempt,
    AttemptEventKind,
    AttemptFailure,
    AttemptUpload,
    CheckpointKind,
    InterventionReason,
    SubmissionCertainty,
    SubmissionEvidence,
    WorkflowState,
)
from applyuminati.core.models.questionnaire import AnswerStatus

__all__ = [
    "CONDITION_REASONS",
    "SUBMISSION_COMPLETES",
    "advance_questions",
    "handle_submission_evidence",
    "handoff_for",
    "mark_submission_attempted",
    "persist_attempt",
    "record_observation",
    "run_form_application",
    "submit_idempotency_key",
    "verify_submission",
]

#: Certainty levels that may close the attempt as submitted.
#: ``LIKELY`` is enough to complete. ``UNCERTAIN`` is not.
SUBMISSION_COMPLETES: frozenset[SubmissionCertainty] = frozenset(
    {SubmissionCertainty.CONFIRMED, SubmissionCertainty.LIKELY}
)

CONDITION_REASONS: dict[PageCondition, InterventionReason] = {
    PageCondition.LOGIN_REQUIRED: InterventionReason.AUTHENTICATION_REQUIRED,
    PageCondition.HUMAN_CHALLENGE: InterventionReason.CAPTCHA_REQUIRED,
    PageCondition.AUTOMATION_BLOCKED: InterventionReason.AUTOMATION_BLOCKED,
}


def record_observation(attempt: ApplicationAttempt, observation: PageObservation) -> None:
    """Store a redacted summary, never the full page."""
    text = observation.text or ""
    attempt.observations.append(
        {
            "url": observation.url,
            "title": observation.title,
            "condition": observation.condition.value,
            "text": text[:500],
            "validation_errors": list(observation.validation_errors),
        }
    )
    if len(attempt.observations) > 40:
        attempt.observations = attempt.observations[-20:]
    attempt.touch()


def handoff_for(attempt: ApplicationAttempt, observation: PageObservation) -> DriverOutcome | None:
    """Open a typed intervention when the page requires a human.

    CAPTCHA, MFA and login walls are handoff, never circumvention.
    """
    if observation.condition not in HANDOFF_CONDITIONS:
        return None
    reason = CONDITION_REASONS.get(observation.condition, InterventionReason.UNKNOWN_INTERACTION)
    instruction = {
        InterventionReason.AUTHENTICATION_REQUIRED: (
            "Sign in, then return here and choose Done, continue."
        ),
        InterventionReason.CAPTCHA_REQUIRED: (
            "Complete the human challenge in the browser. Applyuminati will not bypass it."
        ),
        InterventionReason.AUTOMATION_BLOCKED: (
            "The site is refusing automation. Take over the browser or skip this application."
        ),
        InterventionReason.UNKNOWN_INTERACTION: (
            "The page needs you. Inspect it, then continue or skip."
        ),
    }[reason]
    intervention = attempt.open_intervention(reason, instruction)
    return DriverOutcome(
        kind=DriverOutcomeKind.WAITING_FOR_HUMAN,
        attempt=attempt,
        intervention=intervention,
    )


def advance_questions(
    attempt: ApplicationAttempt,
    observation: PageObservation,
    context: DriverContext,
) -> DriverOutcome | None:
    """Apply questionnaire policy. Returns an outcome when a human must answer."""
    policy = QuestionnairePolicy(context.profile)
    known = {question.key for question in attempt.questions}
    for question in observation.questions:
        if question.key not in known:
            attempt.questions.append(question)
            known.add(question.key)
        decision = policy.decide(question)
        existing = next((a for a in attempt.answers if a.question_key == question.key), None)
        if existing is None:
            attempt.answers.append(decision.draft)
        if decision.paused and decision.draft.status in (
            AnswerStatus.NEEDS_REVIEW,
            AnswerStatus.NEEDS_USER,
        ):
            reason = (
                InterventionReason.LEGAL_ATTESTATION
                if question.sensitivity.value == "legal_attestation"
                else InterventionReason.AMBIGUOUS_QUESTION
            )
            intervention = attempt.open_intervention(
                reason,
                f"Question needs an answer: {question.text}",
                requires_browser_handoff=False,
                question_key=question.key,
                question_text=question.text,
            )
            return DriverOutcome(
                kind=DriverOutcomeKind.WAITING_FOR_HUMAN,
                attempt=attempt,
                intervention=intervention,
            )
    return None


async def apply_ready_answers(
    attempt: ApplicationAttempt, session: BrowserSession, observation: PageObservation
) -> None:
    """Fill fields the policy already approved. Skip anything that needs a human."""
    answers = {draft.question_key: draft for draft in attempt.answers if draft.is_submittable}
    for question in observation.questions:
        draft = answers.get(question.key)
        if draft is None or not question.field_locator or not draft.answer:
            continue
        await session.fill_field(question.field_locator, draft.answer)


async def upload_documents(
    attempt: ApplicationAttempt, session: BrowserSession, context: DriverContext
) -> None:
    permissions = permitted_actions(context.mode, context.profile.strategy)
    check(permissions, "upload_documents")
    observation = context.observation
    if observation is None:
        return
    resume = context.documents.get("resume")
    if resume is None:
        return
    for element in observation.file_inputs():
        result = await session.upload_file(element.locator, resume)
        attempt.uploads.append(
            AttemptUpload(
                kind="resume",
                locator=element.locator,
                relative_path=str(resume),
                confirmed=result.ok,
            )
        )
    if attempt.uploads:
        attempt.record_checkpoint(
            CheckpointKind.DOCUMENTS_UPLOADED.value, summary="resume attached"
        )


def mark_submission_attempted(attempt: ApplicationAttempt) -> None:
    """Record intent before the click so a crash cannot be replayed as a submit."""
    if attempt.submission_attempted_at is None:
        attempt.submission_attempted_at = utcnow()
        attempt.record_event(AttemptEventKind.SUBMITTED, "submission click is about to happen")


def submit_idempotency_key(attempt: ApplicationAttempt) -> str:
    """Stable key for the one final-submit click of this attempt."""
    return f"application-attempt:{attempt.id}:submit"


async def persist_attempt(attempt: ApplicationAttempt, context: DriverContext) -> None:
    """Flush the attempt through the service-owned callback, if one exists."""
    if context.persist is not None:
        await context.persist(attempt)


def verify_submission(observation: PageObservation) -> SubmissionEvidence:
    """Judge confirmation from the page. Honesty over certainty."""
    text = (observation.text or "").lower()
    url = observation.url.lower()
    confirmed_markers = ("application submitted", "thank you for applying", "we received your")
    likely_markers = ("thank you", "application received", "successfully submitted")
    if any(marker in text for marker in confirmed_markers) or "confirmation" in url:
        certainty = SubmissionCertainty.CONFIRMED
    elif any(marker in text for marker in likely_markers):
        certainty = SubmissionCertainty.LIKELY
    else:
        certainty = SubmissionCertainty.UNCERTAIN
    fingerprint = hex(abs(hash((observation.url, text[:200]))))[2:18]
    return SubmissionEvidence(
        certainty=certainty,
        confirmation_url=observation.url,
        confirmation_text=(observation.text or "")[:280],
        text_fingerprint=fingerprint,
        redirect_url=observation.url,
        recorded_at=utcnow(),
    )


def fail(
    attempt: ApplicationAttempt,
    *,
    category: FailureCategory,
    code: str,
    message: str,
    step: str | None = None,
) -> DriverOutcome:
    failure = AttemptFailure(
        category=category,
        code=code,
        message=message,
        driver=attempt.driver,
        step=step or attempt.current_step,
        checkpoint=attempt.latest_checkpoint.kind if attempt.latest_checkpoint else None,
        retryable=category in {FailureCategory.TRANSIENT_NETWORK, FailureCategory.RATE_LIMITED},
        needs_human=category
        in {
            FailureCategory.NEEDS_HUMAN,
            FailureCategory.AUTH_REQUIRED,
            FailureCategory.HUMAN_CHALLENGE,
        },
    )
    attempt.failures.append(failure)
    attempt.workflow_state = WorkflowState.FAILED
    attempt.completed_at = utcnow()
    attempt.record_event(AttemptEventKind.FAILURE, message, category=category.value)
    return DriverOutcome(kind=DriverOutcomeKind.FAILED, attempt=attempt, failure=failure)


def complete(attempt: ApplicationAttempt, evidence: SubmissionEvidence) -> DriverOutcome:
    attempt.evidence = evidence
    attempt.workflow_state = WorkflowState.COMPLETED
    attempt.completed_at = utcnow()
    attempt.record_checkpoint(
        CheckpointKind.SUBMISSION_CONFIRMED.value,
        summary=evidence.certainty.value,
    )
    attempt.record_event(AttemptEventKind.COMPLETED, evidence.certainty.value)
    return DriverOutcome(kind=DriverOutcomeKind.COMPLETED, attempt=attempt, evidence=evidence)


def handle_submission_evidence(
    attempt: ApplicationAttempt, evidence: SubmissionEvidence
) -> DriverOutcome:
    """Apply one submission-evidence rule for both the click path and restarts.

    ``CONFIRMED`` and ``LIKELY`` may complete and record ``SUBMISSION_CONFIRMED``.
    ``UNCERTAIN`` opens ``USER_REVIEW``, keeps ``submission_attempted_at``, and
    never treats the click as success or as a terminal failure.
    """
    attempt.evidence = evidence
    attempt.touch()
    if evidence.certainty in SUBMISSION_COMPLETES:
        return complete(attempt, evidence)
    intervention = attempt.open_intervention(
        InterventionReason.USER_REVIEW,
        (
            "A submit click already happened, but the page does not confirm "
            "receipt. Review the employer site. Do not click submit again."
        ),
        requires_browser_handoff=True,
    )
    return DriverOutcome(
        kind=DriverOutcomeKind.WAITING_FOR_HUMAN,
        attempt=attempt,
        intervention=intervention,
    )


async def agent_still_owns(session: BrowserSession) -> bool:
    """Refuse to act while the human has the browser."""
    owner = await session.control_state()
    return owner is ControlOwner.AGENT


async def run_form_application(
    attempt: ApplicationAttempt,
    session: BrowserSession,
    context: DriverContext,
    *,
    slug: str,
    version: str,
    started_message: str,
    apply_url: str,
    is_submit: Callable[[PageElement], bool],
) -> DriverOutcome:
    """Shared Greenhouse/Lever form flow. Drivers only supply URL and submit matching."""
    attempt.driver = slug
    attempt.driver_version = version
    attempt.workflow_state = WorkflowState.RUNNING
    if attempt.events == []:
        attempt.record_event(AttemptEventKind.STARTED, started_message)

    if attempt.submission_attempted_at is not None:
        observation = await session.observe()
        record_observation(attempt, observation)
        return handle_submission_evidence(attempt, verify_submission(observation))

    observation = await session.navigate(apply_url)
    record_observation(attempt, observation)
    attempt.record_checkpoint(
        CheckpointKind.APPLICATION_OPENED.value,
        url=observation.url,
        summary=observation.title or "",
    )
    context.observation = observation

    blocked = handoff_for(attempt, observation)
    if blocked is not None:
        return blocked

    paused = advance_questions(attempt, observation, context)
    if paused is not None:
        return paused

    permissions = permitted_actions(context.mode, context.profile.strategy)
    check(permissions, "fill_form")
    await apply_ready_answers(attempt, session, observation)
    attempt.record_checkpoint(CheckpointKind.QUESTIONNAIRE_COMPLETE.value)

    if observation.file_inputs():
        await upload_documents(attempt, session, context)

    submit = next((element for element in observation.elements if is_submit(element)), None)
    if submit is None:
        attempt.record_checkpoint(CheckpointKind.REVIEW_PAGE_REACHED.value)
        return DriverOutcome(kind=DriverOutcomeKind.CONTINUED, attempt=attempt)

    try:
        check(permissions, "submit")
    except ActionForbiddenError:
        attempt.record_checkpoint(
            CheckpointKind.REVIEW_PAGE_REACHED.value, summary="fill without submit"
        )
        attempt.workflow_state = WorkflowState.COMPLETED
        return DriverOutcome(kind=DriverOutcomeKind.COMPLETED, attempt=attempt)

    mark_submission_attempted(attempt)
    await persist_attempt(attempt, context)
    clicked = await session.click(
        submit.locator,
        label=submit.label,
        idempotency_key=submit_idempotency_key(attempt),
    )
    observation = await session.observe()
    record_observation(attempt, observation)
    if not clicked.ok:
        return fail(
            attempt,
            category=FailureCategory.EXTRACTION_DRIFT,
            code="application.submit_failed",
            message=clicked.detail or "submit click failed",
        )
    return handle_submission_evidence(attempt, verify_submission(observation))
