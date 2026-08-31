"""Greenhouse ApplicationDriver.

Proves the execution architecture: ATS detection from the apply URL, attempt
creation, capability-matched browser use, questionnaire policy, HITL pause,
checkpoint resume, and submission evidence. Tests drive a fake session, never
a real employer.
"""

from __future__ import annotations

from applyuminati.applications.detect import Detection, detect_ats
from applyuminati.applications.driver import (
    DriverContext,
    DriverMetadata,
    DriverOutcome,
    DriverOutcomeKind,
    application_driver,
)
from applyuminati.applications.modes import ActionForbiddenError, check, permitted_actions
from applyuminati.applications.runner import (
    advance_questions,
    apply_ready_answers,
    complete,
    fail,
    handoff_for,
    mark_submission_attempted,
    record_observation,
    upload_documents,
    verify_submission,
)
from applyuminati.browser.base import BrowserSession, ElementRole
from applyuminati.core.errors import FailureCategory
from applyuminati.core.models.execution import (
    ApplicationAttempt,
    AttemptEventKind,
    CheckpointKind,
    SubmissionCertainty,
    WorkflowState,
)
from applyuminati.core.models.job import AtsVendor

SLUG = "greenhouse"
VERSION = "1"

METADATA = DriverMetadata(
    slug=SLUG,
    name="Greenhouse",
    ats=AtsVendor.GREENHOUSE,
    version=VERSION,
    hosts=frozenset(
        {"boards.greenhouse.io", "job-boards.greenhouse.io", "greenhouse.io"}
    ),
)


class GreenhouseDriver:
    @property
    def metadata(self) -> DriverMetadata:
        return METADATA

    def detects(self, url: str) -> Detection:
        detection = detect_ats(url)
        if detection.ats is AtsVendor.GREENHOUSE:
            return detection
        return Detection(AtsVendor.UNKNOWN, confidence=0.0, host=detection.host)

    async def run(
        self,
        attempt: ApplicationAttempt,
        session: BrowserSession,
        context: DriverContext,
    ) -> DriverOutcome:
        attempt.driver = SLUG
        attempt.driver_version = VERSION
        attempt.workflow_state = WorkflowState.RUNNING
        if attempt.events == []:
            attempt.record_event(AttemptEventKind.STARTED, "greenhouse application opened")

        if attempt.submission_attempted_at is not None:
            observation = await session.observe()
            record_observation(attempt, observation)
            evidence = verify_submission(observation)
            if evidence.certainty is SubmissionCertainty.UNCERTAIN:
                return fail(
                    attempt,
                    category=FailureCategory.DUPLICATE_ACTION,
                    code="application.submission_uncertain",
                    message="submission was attempted earlier; confirmation is still uncertain",
                )
            return complete(attempt, evidence)

        url = context.job.apply_url or context.job.canonical_url
        observation = await session.navigate(url)
        record_observation(attempt, observation)
        attempt.record_checkpoint(
            CheckpointKind.APPLICATION_OPENED.value, url=observation.url, summary=observation.title or ""
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

        submit = next(
            (
                element
                for element in observation.elements
                if element.role is ElementRole.BUTTON
                and element.label
                and "submit" in element.label.lower()
            ),
            None,
        )
        if submit is None:
            attempt.record_checkpoint(CheckpointKind.REVIEW_PAGE_REACHED.value)
            return DriverOutcome(kind=DriverOutcomeKind.CONTINUED, attempt=attempt)

        try:
            check(permissions, "submit")
        except ActionForbiddenError:
            attempt.record_checkpoint(CheckpointKind.REVIEW_PAGE_REACHED.value, summary="fill without submit")
            attempt.workflow_state = WorkflowState.COMPLETED
            return DriverOutcome(kind=DriverOutcomeKind.COMPLETED, attempt=attempt)

        mark_submission_attempted(attempt)
        clicked = await session.click(submit.locator, label=submit.label)
        observation = await session.observe()
        record_observation(attempt, observation)
        if not clicked.ok:
            return fail(
                attempt,
                category=FailureCategory.EXTRACTION_DRIFT,
                code="application.submit_failed",
                message=clicked.detail or "submit click failed",
            )
        return complete(attempt, verify_submission(observation))


def _create() -> GreenhouseDriver:
    return GreenhouseDriver()


PLUGIN = application_driver(
    slug=SLUG,
    name=METADATA.name,
    factory=_create,
    ats=AtsVendor.GREENHOUSE,
    description="Greenhouse application workflow. Detection is from the apply URL.",
    priority=50,
)
