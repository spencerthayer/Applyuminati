"""Persistence for ApplicationAttempt aggregates."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.models.execution import ApplicationAttempt, WorkflowState
from applyuminati.db.models import ApplicationAttemptRow

__all__ = ["AttemptRepository"]


def _to_record(row: ApplicationAttemptRow) -> ApplicationAttempt:
    payload = row.payload or {}
    return ApplicationAttempt.model_validate(
        {
            "id": row.id,
            "application_id": row.application_id,
            "job_id": row.job_id,
            "profile_id": row.profile_id,
            "driver": row.driver,
            "driver_version": row.driver_version,
            "workflow_state": row.workflow_state,
            "current_step": row.current_step,
            "submission_mode": row.submission_mode,
            "browser_host_id": row.browser_host_id,
            "browser_backend": row.browser_backend,
            "browser_session_id": row.browser_session_id,
            "task_space_id": row.task_space_id,
            "task_space_numeric_id": row.task_space_numeric_id,
            "started_at": row.started_at,
            "updated_at": row.updated_at,
            "completed_at": row.completed_at,
            "submission_attempted_at": row.submission_attempted_at,
            "checkpoints": payload.get("checkpoints", []),
            "questions": payload.get("questions", []),
            "answers": payload.get("answers", []),
            "uploads": payload.get("uploads", []),
            "interventions": payload.get("interventions", []),
            "failures": payload.get("failures", []),
            "events": payload.get("events", []),
            "evidence": payload.get("evidence", {}),
            "observations": payload.get("observations", []),
        }
    )


def _payload(record: ApplicationAttempt) -> dict:
    return {
        "checkpoints": [item.model_dump(mode="json") for item in record.checkpoints],
        "questions": [item.model_dump(mode="json") for item in record.questions],
        "answers": [item.model_dump(mode="json") for item in record.answers],
        "uploads": [item.model_dump(mode="json") for item in record.uploads],
        "interventions": [item.model_dump(mode="json") for item in record.interventions],
        "failures": [item.model_dump(mode="json") for item in record.failures],
        "events": [item.model_dump(mode="json") for item in record.events],
        "evidence": record.evidence.model_dump(mode="json"),
        "observations": list(record.observations),
    }


def _apply(record: ApplicationAttempt, row: ApplicationAttemptRow) -> ApplicationAttemptRow:
    row.application_id = record.application_id
    row.job_id = record.job_id
    row.profile_id = record.profile_id
    row.driver = record.driver
    row.driver_version = record.driver_version
    row.workflow_state = record.workflow_state.value
    row.current_step = record.current_step
    row.submission_mode = record.submission_mode.value
    row.browser_host_id = record.browser_host_id
    row.browser_backend = record.browser_backend
    row.browser_session_id = record.browser_session_id
    row.task_space_id = record.task_space_id
    row.task_space_numeric_id = record.task_space_numeric_id
    row.started_at = record.started_at
    row.updated_at = record.updated_at
    row.completed_at = record.completed_at
    row.submission_attempted_at = record.submission_attempted_at
    row.payload = _payload(record)
    return row


class AttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, record: ApplicationAttempt) -> ApplicationAttempt:
        row = await self._session.get(ApplicationAttemptRow, record.id)
        if row is None:
            row = ApplicationAttemptRow(id=record.id)
            self._session.add(row)
        _apply(record, row)
        await self._session.flush()
        return record

    async def get(self, attempt_id: str) -> ApplicationAttempt | None:
        row = await self._session.get(ApplicationAttemptRow, attempt_id)
        return _to_record(row) if row else None

    async def list_for_application(self, application_id: str) -> list[ApplicationAttempt]:
        rows = (
            await self._session.scalars(
                select(ApplicationAttemptRow)
                .where(ApplicationAttemptRow.application_id == application_id)
                .order_by(ApplicationAttemptRow.started_at.desc())
            )
        ).all()
        return [_to_record(row) for row in rows]

    async def list_waiting(self) -> list[ApplicationAttempt]:
        rows = (
            await self._session.scalars(
                select(ApplicationAttemptRow)
                .where(ApplicationAttemptRow.workflow_state == WorkflowState.WAITING_FOR_HUMAN.value)
                .order_by(ApplicationAttemptRow.updated_at.desc())
            )
        ).all()
        return [_to_record(row) for row in rows]

    async def list(
        self, *, states: Sequence[WorkflowState] | None = None
    ) -> list[ApplicationAttempt]:
        statement = select(ApplicationAttemptRow)
        if states:
            statement = statement.where(
                ApplicationAttemptRow.workflow_state.in_([state.value for state in states])
            )
        rows = (
            await self._session.scalars(statement.order_by(ApplicationAttemptRow.updated_at.desc()))
        ).all()
        return [_to_record(row) for row in rows]
