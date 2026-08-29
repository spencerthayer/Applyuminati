"""Application lifecycle: states, transitions and the event log.

The mutable ``state`` column is a cache. The **event log is the record**:
every transition is appended with who caused it, why, and what evidence
existed at the time, so an application's history is auditable and a failed
run can be resumed from the last known-good point.

The transition table is declared once, here, and enforced by
:func:`can_transition`. Adding a state without wiring its transitions raises
at import time (see the completeness check at the bottom of the module).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.errors import FailureCategory
from applyuminati.core.ids import new_ulid


class ApplicationState(StrEnum):
    """Canonical application states."""

    DISCOVERED = "discovered"
    EVALUATING = "evaluating"
    SKIPPED = "skipped"
    SHORTLISTED = "shortlisted"
    PREPARING = "preparing"
    READY = "ready"
    APPLYING = "applying"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    RECRUITER_CONTACT = "recruiter_contact"
    ASSESSMENT = "assessment"
    INTERVIEW = "interview"
    FOLLOW_UP = "follow_up"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    OFFER = "offer"
    ACCEPTED = "accepted"
    CLOSED = "closed"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"


#: States after which no further automated progress is expected.
TERMINAL_STATES: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.SKIPPED,
        ApplicationState.REJECTED,
        ApplicationState.WITHDRAWN,
        ApplicationState.ACCEPTED,
        ApplicationState.CLOSED,
    }
)

#: States meaning "an application exists at the employer". Used to make
#: submission idempotent: we never re-apply from one of these.
SUBMITTED_STATES: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.SUBMITTED,
        ApplicationState.CONFIRMED,
        ApplicationState.RECRUITER_CONTACT,
        ApplicationState.ASSESSMENT,
        ApplicationState.INTERVIEW,
        ApplicationState.FOLLOW_UP,
        ApplicationState.OFFER,
        ApplicationState.ACCEPTED,
    }
)

#: Non-terminal states a human must resolve.
ATTENTION_STATES: frozenset[ApplicationState] = frozenset(
    {ApplicationState.NEEDS_ATTENTION, ApplicationState.FAILED}
)

_S = ApplicationState

#: Allowed transitions. Anything absent here is rejected by :func:`can_transition`.
TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    _S.DISCOVERED: frozenset({_S.EVALUATING, _S.SKIPPED, _S.SHORTLISTED, _S.FAILED, _S.CLOSED}),
    _S.EVALUATING: frozenset(
        {_S.SHORTLISTED, _S.SKIPPED, _S.NEEDS_ATTENTION, _S.FAILED, _S.CLOSED}
    ),
    _S.SKIPPED: frozenset({_S.EVALUATING, _S.SHORTLISTED, _S.CLOSED}),
    _S.SHORTLISTED: frozenset({_S.PREPARING, _S.SKIPPED, _S.WITHDRAWN, _S.FAILED, _S.CLOSED}),
    _S.PREPARING: frozenset({_S.READY, _S.NEEDS_ATTENTION, _S.FAILED, _S.WITHDRAWN, _S.SKIPPED}),
    _S.READY: frozenset({_S.APPLYING, _S.PREPARING, _S.WITHDRAWN, _S.FAILED, _S.SKIPPED}),
    _S.APPLYING: frozenset({_S.SUBMITTED, _S.NEEDS_ATTENTION, _S.FAILED, _S.READY, _S.WITHDRAWN}),
    _S.SUBMITTED: frozenset(
        {
            _S.CONFIRMED,
            _S.RECRUITER_CONTACT,
            _S.ASSESSMENT,
            _S.INTERVIEW,
            _S.REJECTED,
            _S.WITHDRAWN,
            _S.FOLLOW_UP,
            _S.NEEDS_ATTENTION,
            _S.CLOSED,
        }
    ),
    _S.CONFIRMED: frozenset(
        {
            _S.RECRUITER_CONTACT,
            _S.ASSESSMENT,
            _S.INTERVIEW,
            _S.REJECTED,
            _S.WITHDRAWN,
            _S.FOLLOW_UP,
            _S.CLOSED,
        }
    ),
    _S.RECRUITER_CONTACT: frozenset(
        {_S.ASSESSMENT, _S.INTERVIEW, _S.FOLLOW_UP, _S.REJECTED, _S.WITHDRAWN, _S.CLOSED}
    ),
    _S.ASSESSMENT: frozenset(
        {_S.INTERVIEW, _S.REJECTED, _S.FOLLOW_UP, _S.WITHDRAWN, _S.NEEDS_ATTENTION, _S.CLOSED}
    ),
    _S.INTERVIEW: frozenset(
        {_S.INTERVIEW, _S.OFFER, _S.REJECTED, _S.FOLLOW_UP, _S.WITHDRAWN, _S.ASSESSMENT, _S.CLOSED}
    ),
    _S.FOLLOW_UP: frozenset(
        {
            _S.INTERVIEW,
            _S.ASSESSMENT,
            _S.RECRUITER_CONTACT,
            _S.OFFER,
            _S.REJECTED,
            _S.WITHDRAWN,
            _S.CLOSED,
        }
    ),
    _S.OFFER: frozenset({_S.ACCEPTED, _S.REJECTED, _S.WITHDRAWN, _S.FOLLOW_UP, _S.CLOSED}),
    _S.ACCEPTED: frozenset({_S.CLOSED}),
    _S.REJECTED: frozenset({_S.CLOSED, _S.FOLLOW_UP}),
    _S.WITHDRAWN: frozenset({_S.CLOSED}),
    _S.CLOSED: frozenset(),
    # Recovery states can rejoin the pipeline once a human or a retry resolves them.
    _S.FAILED: frozenset(
        {_S.NEEDS_ATTENTION, _S.SHORTLISTED, _S.PREPARING, _S.READY, _S.APPLYING, _S.CLOSED}
    ),
    _S.NEEDS_ATTENTION: frozenset(
        {
            _S.EVALUATING,
            _S.SHORTLISTED,
            _S.PREPARING,
            _S.READY,
            _S.APPLYING,
            _S.SUBMITTED,
            _S.SKIPPED,
            _S.WITHDRAWN,
            _S.FAILED,
            _S.CLOSED,
        }
    ),
}

_missing = set(ApplicationState) - set(TRANSITIONS)
if _missing:  # pragma: no cover - guards a developer mistake at import time
    msg = f"ApplicationState members without a transition entry: {sorted(_missing)}"
    raise RuntimeError(msg)


def can_transition(current: ApplicationState, target: ApplicationState) -> bool:
    """Return ``True`` when ``current -> target`` is a legal transition."""
    if current is target:
        return True
    return target in TRANSITIONS[current]


def allowed_transitions(current: ApplicationState) -> list[ApplicationState]:
    return sorted(TRANSITIONS[current], key=lambda s: s.value)


class ActorKind(StrEnum):
    """Who caused an event. Automation and humans are never conflated."""

    USER = "user"
    SYSTEM = "system"
    LLM = "llm"
    AGENT_BACKEND = "agent_backend"
    BROWSER = "browser"
    EMAIL = "email"
    SOURCE = "source"


class ApplicationEvent(BaseModel):
    """One immutable entry in an application's history."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    application_id: str
    occurred_at: datetime = Field(default_factory=utcnow)
    from_state: ApplicationState | None = None
    to_state: ApplicationState | None = None
    actor: ActorKind = ActorKind.SYSTEM
    #: Specific actor identity: plugin slug, model id, agent backend, "cli".
    actor_detail: str | None = None
    #: Short machine-readable reason, e.g. ``score.below_threshold``.
    reason: str = ""
    #: Free-text detail for the UI.
    message: str | None = None
    #: Structured, redaction-safe payload. Never carries answers or secrets.
    data: dict[str, Any] = Field(default_factory=dict)
    #: Populated when the event records a failure.
    failure_category: FailureCategory | None = None
    #: Correlates the event with a task/run in the observability log.
    run_id: str | None = None
    task_id: str | None = None

    @property
    def is_transition(self) -> bool:
        return self.to_state is not None and self.to_state is not self.from_state


class ApplicationArtifact(BaseModel):
    """A file produced for or during an application."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    kind: str  # "resume_pdf", "resume_json", "cover_letter", "screenshot", "dom_snapshot"
    #: Path relative to the data directory, never absolute in the database.
    relative_path: str
    content_type: str | None = None
    bytes_written: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    #: Ids of the claims this artifact's content is grounded in.
    evidence_claim_ids: list[str] = Field(default_factory=list)


class Application(BaseModel):
    """The user's pursuit of one job."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    job_id: str
    profile_id: str
    state: ApplicationState = ApplicationState.DISCOVERED
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    #: Set the moment we have evidence the employer received an application.
    submitted_at: datetime | None = None
    #: Employer-side reference, when one is shown or emailed.
    external_reference: str | None = None
    #: Most recent fit score id, for display without a join.
    fit_score_id: str | None = None
    #: Idempotency guard: hash of (profile, employer, role) written on submit.
    submission_fingerprint: str | None = None
    notes: str | None = None
    events: list[ApplicationEvent] = Field(default_factory=list)
    artifacts: list[ApplicationArtifact] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def needs_attention(self) -> bool:
        return self.state in ATTENTION_STATES

    @property
    def already_submitted(self) -> bool:
        """True when an application demonstrably exists at the employer."""
        return self.state in SUBMITTED_STATES

    def last_event(self) -> ApplicationEvent | None:
        return self.events[-1] if self.events else None


__all__ = [
    "ATTENTION_STATES",
    "SUBMITTED_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "ActorKind",
    "Application",
    "ApplicationArtifact",
    "ApplicationEvent",
    "ApplicationState",
    "allowed_transitions",
    "can_transition",
]
