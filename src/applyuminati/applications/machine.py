"""Application state machine: transitions are auditable, the log is the record.

Every state change goes through :meth:`ApplicationMachine.transition`, which
validates against the :data:`TRANSITIONS` table and raises with the legal
targets named when the transition is illegal. The event is appended before the
cached ``state`` column is updated, so a crash between the two leaves the log
authoritative and :meth:`replay` can repair the cache.
"""

from __future__ import annotations

from applyuminati.core.clock import utcnow
from applyuminati.core.errors import ApplyuminatiError, FailureCategory
from applyuminati.core.ids import new_ulid
from applyuminati.core.models.application import (
    SUBMITTED_STATES,
    ActorKind,
    Application,
    ApplicationEvent,
    ApplicationState,
    allowed_transitions,
    can_transition,
)

__all__ = ["ApplicationMachine"]


class IllegalTransitionError(ApplyuminatiError):
    category = FailureCategory.CONFIGURATION


class ApplicationMachine:
    """Guard and record every application state change."""

    def transition(
        self,
        application: Application,
        to_state: ApplicationState,
        *,
        actor: ActorKind = ActorKind.SYSTEM,
        actor_detail: str | None = None,
        reason: str = "",
        message: str | None = None,
        data: dict[str, object] | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        failure_category: FailureCategory | None = None,
    ) -> ApplicationEvent:
        """Validate and apply a transition, returning the event record.

        Raises :class:`IllegalTransitionError` listing the legal targets when
        the transition is not in :data:`TRANSITIONS`.
        """
        from_state = application.state
        if not can_transition(from_state, to_state):
            legal = [state.value for state in allowed_transitions(from_state)]
            raise IllegalTransitionError(
                f"illegal transition {from_state.value} -> {to_state.value}; "
                f"legal targets from {from_state.value}: {legal}",
                code="application.illegal_transition",
                details={
                    "from_state": from_state.value,
                    "to_state": to_state.value,
                    "legal_targets": legal,
                },
            )

        event = ApplicationEvent(
            id=new_ulid(),
            application_id=application.id,
            occurred_at=utcnow(),
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            actor_detail=actor_detail,
            reason=reason,
            message=message,
            data=data or {},
            failure_category=failure_category,
            run_id=run_id,
            task_id=task_id,
        )
        application.events.append(event)
        application.state = to_state
        application.updated_at = utcnow()
        if to_state in SUBMITTED_STATES and application.submitted_at is None:
            application.submitted_at = utcnow()
        return event

    @staticmethod
    def replay(events: list[ApplicationEvent]) -> ApplicationState:
        """Rebuild state purely from the event log.

        Proves the log is the record and the column is a cache: replaying the
        full history must yield the same state as the cached column.
        """
        state = ApplicationState.DISCOVERED
        for event in events:
            if event.to_state is not None:
                state = event.to_state
        return state

    @staticmethod
    def history_summary(application: Application) -> list[str]:
        """One-line-per-event rendering for the UI."""
        lines: list[str] = []
        for event in application.events:
            actor = event.actor_detail or event.actor.value
            from_label = event.from_state.value if event.from_state else "(none)"
            to_label = event.to_state.value if event.to_state else "(none)"
            arrow = f"{from_label} -> {to_label}" if event.from_state else f"-> {to_label}"
            lines.append(f"[{event.occurred_at.isoformat()}] {actor}: {arrow} ({event.reason})")
        return lines
