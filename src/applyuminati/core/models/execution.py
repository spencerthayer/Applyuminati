"""Application attempts, workflow state, and human intervention.

``ApplicationState`` answers where a pursuit is in the hiring process.
``WorkflowState`` answers what the executor is doing right now. They are
orthogonal: an application can be APPLYING while the workflow is
WAITING_FOR_HUMAN, and SUBMITTED while the workflow is WAITING_FOR_PROVIDER.

WAITING_FOR_HUMAN is not a failure. It never enters retry policy and never
counts against ``max_attempts``. Entering it is a persistence operation: the
worker writes the checkpoint, records the intervention, releases the lease,
and leaves. Resume is a new claim after the user acts.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from applyuminati.core.clock import utcnow
from applyuminati.core.errors import FailureCategory
from applyuminati.core.ids import new_ulid
from applyuminati.core.models.questionnaire import AnswerDraft, ApplicationQuestion
from applyuminati.core.settings import ExecutionMode


class WorkflowState(StrEnum):
    """What the executor is doing. Distinct from :class:`ApplicationState`."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_HUMAN = "waiting_for_human"
    WAITING_FOR_PROVIDER = "waiting_for_provider"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


WORKFLOW_TERMINAL: frozenset[WorkflowState] = frozenset(
    {WorkflowState.COMPLETED, WorkflowState.FAILED, WorkflowState.CANCELLED}
)

#: States that must not hold a worker. The lease is released and the task
#: leaves the queue; nothing sits in memory waiting for a human or a provider.
WORKFLOW_RELEASES_WORKER: frozenset[WorkflowState] = frozenset(
    {
        WorkflowState.WAITING_FOR_HUMAN,
        WorkflowState.WAITING_FOR_PROVIDER,
        WorkflowState.RETRY_SCHEDULED,
        WorkflowState.COMPLETED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
        WorkflowState.PENDING,
    }
)


class InterventionReason(StrEnum):
    """Why a human is needed. Typed so the inbox can group and instruct."""

    AUTHENTICATION_REQUIRED = "authentication_required"
    CAPTCHA_REQUIRED = "captcha_required"
    MFA_REQUIRED = "mfa_required"
    IDENTITY_VERIFICATION = "identity_verification"
    LEGAL_ATTESTATION = "legal_attestation"
    AMBIGUOUS_QUESTION = "ambiguous_question"
    DOCUMENT_REQUIRED = "document_required"
    PAYMENT_OR_FEE = "payment_or_fee"
    USER_REVIEW = "user_review"
    AUTOMATION_BLOCKED = "automation_blocked"
    UNKNOWN_INTERACTION = "unknown_interaction"


#: Reasons that transfer browser ownership. Overridable per intervention.
BROWSER_HANDOFF_REASONS: frozenset[InterventionReason] = frozenset(
    {
        InterventionReason.AUTHENTICATION_REQUIRED,
        InterventionReason.CAPTCHA_REQUIRED,
        InterventionReason.MFA_REQUIRED,
        InterventionReason.IDENTITY_VERIFICATION,
        InterventionReason.AUTOMATION_BLOCKED,
        InterventionReason.UNKNOWN_INTERACTION,
        InterventionReason.USER_REVIEW,
    }
)


class InterventionResolution(StrEnum):
    DONE_CONTINUE = "done_continue"
    SKIP_APPLICATION = "skip_application"
    KEEP_CONTROL = "keep_control"
    ANSWER = "answer"
    APPROVE = "approve"
    REJECT = "reject"
    PROVIDE_DOCUMENT = "provide_document"
    CANCEL = "cancel"


class CheckpointKind(StrEnum):
    """Shared recovery vocabulary. Drivers may add ``driver:`` prefixes."""

    APPLICATION_OPENED = "application_opened"
    ACCOUNT_AUTHENTICATED = "account_authenticated"
    PERSONAL_INFORMATION_COMPLETE = "personal_information_complete"
    EMPLOYMENT_HISTORY_COMPLETE = "employment_history_complete"
    EDUCATION_COMPLETE = "education_complete"
    QUESTIONNAIRE_COMPLETE = "questionnaire_complete"
    DOCUMENTS_UPLOADED = "documents_uploaded"
    REVIEW_PAGE_REACHED = "review_page_reached"
    SUBMISSION_CONFIRMED = "submission_confirmed"


class SubmissionCertainty(StrEnum):
    """How sure we are that the employer received the application.

    Clicking the final button is not confirmation. A workflow that cannot
    verify must say so rather than invent certainty.
    """

    CONFIRMED = "confirmed"
    LIKELY = "likely"
    UNCERTAIN = "uncertain"
    NOT_ATTEMPTED = "not_attempted"


class AttemptEventKind(StrEnum):
    STARTED = "started"
    CHECKPOINT = "checkpoint"
    INTERVENTION_OPENED = "intervention_opened"
    INTERVENTION_RESOLVED = "intervention_resolved"
    FAILURE = "failure"
    RESUMED = "resumed"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class HumanIntervention(BaseModel):
    """A typed pause. Never routed through retry policy."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    reason: InterventionReason
    instruction: str
    #: Defaulted from the reason; overridable when a CAPTCHA is actually a form
    #: question or an attestation lives on a page the agent still owns.
    requires_browser_handoff: bool = False
    question_key: str | None = None
    question_text: str | None = None
    opened_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None
    resolution: InterventionResolution | None = None
    resolution_payload: dict[str, Any] = Field(default_factory=dict)
    #: Browser identity the user should open. Empty when the pause is in-app.
    browser_host_id: str | None = None
    browser_session_id: str | None = None
    task_space_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _default_handoff(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "requires_browser_handoff" not in data:
            reason = data.get("reason")
            if reason is not None:
                reason_value = (
                    reason if isinstance(reason, InterventionReason) else InterventionReason(reason)
                )
                data = {
                    **data,
                    "requires_browser_handoff": reason_value in BROWSER_HANDOFF_REASONS,
                }
        return data

    @property
    def open(self) -> bool:
        return self.resolved_at is None

    def resolve(
        self,
        resolution: InterventionResolution,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.resolution = resolution
        self.resolution_payload = payload or {}
        self.resolved_at = utcnow()


class AttemptCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    kind: str
    step: str | None = None
    url: str | None = None
    summary: str = ""
    recorded_at: datetime = Field(default_factory=utcnow)
    #: Backend-opaque resume handle (ego lite task-space id, storage state).
    backend_state: dict[str, Any] = Field(default_factory=dict)


class AttemptUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    kind: str
    locator: str | None = None
    relative_path: str
    confirmed: bool = False
    uploaded_at: datetime = Field(default_factory=utcnow)


class AttemptFailure(BaseModel):
    """A structured failure. Not a string, not an exception."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    category: FailureCategory
    code: str
    message: str
    driver: str | None = None
    step: str | None = None
    checkpoint: str | None = None
    retryable: bool = False
    needs_human: bool = False
    recovery_attempted: str | None = None
    occurred_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


class SubmissionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    certainty: SubmissionCertainty = SubmissionCertainty.NOT_ATTEMPTED
    confirmation_url: str | None = None
    confirmation_id: str | None = None
    confirmation_text: str | None = None
    #: Short fingerprint of the confirmation text, for later matching.
    text_fingerprint: str | None = None
    redirect_url: str | None = None
    recorded_at: datetime | None = None
    notes: str = ""


class AttemptEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    kind: AttemptEventKind
    at: datetime = Field(default_factory=utcnow)
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ApplicationAttempt(BaseModel):
    """One try at one application. The durable execution aggregate."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    application_id: str
    job_id: str
    profile_id: str | None = None

    driver: str
    driver_version: str = "1"
    workflow_state: WorkflowState = WorkflowState.PENDING
    current_step: str | None = None
    submission_mode: ExecutionMode = ExecutionMode.FILL_NO_SUBMIT

    browser_host_id: str | None = None
    browser_backend: str | None = None
    browser_session_id: str | None = None
    #: ego lite name (``applyuminati:<attempt id>``) and, once learned, numeric id.
    task_space_id: str | None = None
    task_space_numeric_id: int | None = None

    started_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    #: Set *before* the final click so a crash cannot be replayed as a submit.
    submission_attempted_at: datetime | None = None

    checkpoints: list[AttemptCheckpoint] = Field(default_factory=list)
    questions: list[ApplicationQuestion] = Field(default_factory=list)
    answers: list[AnswerDraft] = Field(default_factory=list)
    uploads: list[AttemptUpload] = Field(default_factory=list)
    interventions: list[HumanIntervention] = Field(default_factory=list)
    failures: list[AttemptFailure] = Field(default_factory=list)
    events: list[AttemptEvent] = Field(default_factory=list)
    evidence: SubmissionEvidence = Field(default_factory=SubmissionEvidence)
    #: Redacted page summaries, never full DOM dumps.
    observations: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.workflow_state in WORKFLOW_TERMINAL

    @property
    def pending_intervention(self) -> HumanIntervention | None:
        return next((item for item in self.interventions if item.open), None)

    @property
    def latest_checkpoint(self) -> AttemptCheckpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    def touch(self) -> None:
        self.updated_at = utcnow()

    def record_event(self, kind: AttemptEventKind, message: str = "", **data: Any) -> AttemptEvent:
        event = AttemptEvent(kind=kind, message=message, data=data)
        self.events.append(event)
        self.touch()
        return event

    def record_checkpoint(
        self,
        kind: str,
        *,
        step: str | None = None,
        url: str | None = None,
        summary: str = "",
        backend_state: dict[str, Any] | None = None,
    ) -> AttemptCheckpoint:
        checkpoint = AttemptCheckpoint(
            kind=kind,
            step=step,
            url=url,
            summary=summary,
            backend_state=backend_state or {},
        )
        self.checkpoints.append(checkpoint)
        self.current_step = step or kind
        self.record_event(AttemptEventKind.CHECKPOINT, kind, step=step or "")
        return checkpoint

    def open_intervention(
        self,
        reason: InterventionReason,
        instruction: str,
        *,
        requires_browser_handoff: bool | None = None,
        question_key: str | None = None,
        question_text: str | None = None,
    ) -> HumanIntervention:
        intervention = HumanIntervention(
            reason=reason,
            instruction=instruction,
            question_key=question_key,
            question_text=question_text,
            browser_host_id=self.browser_host_id,
            browser_session_id=self.browser_session_id,
            task_space_id=self.task_space_id,
        )
        if requires_browser_handoff is not None:
            intervention.requires_browser_handoff = requires_browser_handoff
        self.interventions.append(intervention)
        self.workflow_state = WorkflowState.WAITING_FOR_HUMAN
        self.record_event(
            AttemptEventKind.INTERVENTION_OPENED,
            instruction,
            reason=reason.value,
            intervention_id=intervention.id,
        )
        return intervention


__all__ = [
    "BROWSER_HANDOFF_REASONS",
    "WORKFLOW_RELEASES_WORKER",
    "WORKFLOW_TERMINAL",
    "ApplicationAttempt",
    "AttemptCheckpoint",
    "AttemptEvent",
    "AttemptEventKind",
    "AttemptFailure",
    "AttemptUpload",
    "CheckpointKind",
    "HumanIntervention",
    "InterventionReason",
    "InterventionResolution",
    "SubmissionCertainty",
    "SubmissionEvidence",
    "WorkflowState",
]
