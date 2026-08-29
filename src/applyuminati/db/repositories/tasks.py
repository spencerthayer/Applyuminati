"""Durable task persistence with lease-based claiming.

SQLite has no ``SELECT ... FOR UPDATE``, so claiming is done as an
optimistic UPDATE gated on the row's current state: only one worker's update
can affect the row, and everyone else sees ``rowcount == 0`` and moves on.
This is the same pattern Postgres needs for a queue without advisory locks,
so the logic transfers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.clock import utcnow
from applyuminati.core.models.task import TaskAttempt, TaskRecord, TaskState
from applyuminati.db.mappers import row_to_task, task_to_row
from applyuminati.db.models import TaskRow


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, task: TaskRecord) -> TaskRecord:
        """Insert, or return the existing pending task with the same key."""
        if task.idempotency_key:
            existing = (
                await self._session.scalars(
                    select(TaskRow).where(
                        TaskRow.idempotency_key == task.idempotency_key,
                        TaskRow.state.in_([TaskState.PENDING.value, TaskState.RUNNING.value]),
                    )
                )
            ).first()
            if existing is not None:
                return row_to_task(existing)
        self._session.add(task_to_row(task))
        await self._session.flush()
        return task

    async def claim_next(
        self, *, kinds: Sequence[str] | None = None, lease_seconds: int = 300
    ) -> TaskRecord | None:
        """Atomically claim the due-est pending task.

        The two-step claim (SELECT then conditional UPDATE) is safe under
        concurrency because the UPDATE re-checks state: whichever worker's
        update lands first wins and the others see ``rowcount == 0``.
        """
        now = utcnow()
        statement = select(TaskRow.id).where(
            TaskRow.state == TaskState.PENDING.value, TaskRow.scheduled_for <= now
        )
        if kinds:
            statement = statement.where(TaskRow.kind.in_(list(kinds)))
        statement = statement.order_by(TaskRow.priority.desc(), TaskRow.scheduled_for).limit(1)
        task_id = await self._session.scalar(statement)
        if task_id is None:
            return None

        result = cast(
            CursorResult,
            await self._session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.state == TaskState.PENDING.value)
                .values(
                    state=TaskState.RUNNING.value,
                    started_at=now,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
            ),
        )
        if result.rowcount != 1:  # pragma: no cover - loses only under contention
            return None
        row = await self._session.get(TaskRow, task_id)
        return row_to_task(row) if row else None

    async def save(self, task: TaskRecord) -> TaskRecord:
        row = await self._session.get(TaskRow, task.id)
        if row is None:
            row = TaskRow(id=task.id)
            self._session.add(row)
        task_to_row(task, row=row)
        await self._session.flush()
        return task

    async def get(self, task_id: str) -> TaskRecord | None:
        row = await self._session.get(TaskRow, task_id)
        return row_to_task(row) if row else None

    async def list(
        self,
        *,
        states: Sequence[TaskState] | None = None,
        run_id: str | None = None,
        limit: int = 50,
    ) -> list[TaskRecord]:
        statement = select(TaskRow)
        if states:
            statement = statement.where(TaskRow.state.in_([state.value for state in states]))
        if run_id is not None:
            statement = statement.where(TaskRow.run_id == run_id)
        statement = statement.order_by(TaskRow.created_at.desc()).limit(limit)
        rows = (await self._session.scalars(statement)).all()
        return [row_to_task(row) for row in rows]

    async def reclaim_expired_leases(self) -> int:
        """Return ``running`` tasks whose lease lapsed to ``pending``.

        A crashed worker must not wedge its tasks forever; the attempt list on
        each task records the lost lease so the recovery policy sees it.
        """
        now = utcnow()
        rows = await self._session.scalars(
            select(TaskRow).where(
                TaskRow.state == TaskState.RUNNING.value,
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at < now,
            )
        )
        reclaimed = 0
        for row in rows.all():
            task = row_to_task(row)
            task.attempts.append(
                TaskAttempt(
                    attempt=task.attempt_count + 1,
                    succeeded=False,
                    strategy=None,
                    failure_message="lease expired before the worker finished",
                )
            )
            task.state = TaskState.PENDING
            task.lease_expires_at = None
            task.scheduled_for = now
            task_to_row(task, row=row)
            reclaimed += 1
        await self._session.flush()
        return reclaimed


__all__ = ["TaskRepository"]
