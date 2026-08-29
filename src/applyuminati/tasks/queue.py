"""Task queue: submit, claim, complete, fail — with self-healing policy.

``fail`` is where recovery decisions are made. The :func:`recovery.decide`
function consults the failure category and the strategies already tried, then
the queue sets the task to ``RETRYING`` (with backoff), ``FAILED``, or
``BLOCKED``. A task that has tried every registered strategy for its kind
cannot loop — it goes terminal.
"""

from __future__ import annotations

from typing import Any

from applyuminati.core.clock import utcnow
from applyuminati.core.errors import ApplyuminatiError
from applyuminati.core.ids import new_ulid
from applyuminati.core.models.task import TaskAttempt, TaskRecord, TaskState
from applyuminati.db.repositories.tasks import TaskRepository
from applyuminati.tasks.recovery import decide

__all__ = ["TaskQueue"]


class TaskQueue:
    def __init__(self, repo: TaskRepository) -> None:
        self._repo = repo

    async def submit(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        scheduled_for: Any | None = None,
    ) -> TaskRecord:
        task = TaskRecord(
            id=new_ulid(),
            run_id=run_id,
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
            priority=priority,
            max_attempts=max_attempts,
            scheduled_for=scheduled_for or utcnow(),
        )
        return await self._repo.enqueue(task)

    async def claim(
        self, *, kinds: list[str] | None = None, lease_seconds: int = 300
    ) -> TaskRecord | None:
        return await self._repo.claim_next(kinds=kinds, lease_seconds=lease_seconds)

    async def checkpoint(self, task: TaskRecord, resume_state: dict[str, Any]) -> TaskRecord:
        """Persist partial progress immediately, without changing task state."""
        task.resume_state = resume_state
        task.updated_at = utcnow()
        return await self._repo.save(task)

    async def complete(self, task: TaskRecord, result: dict[str, Any]) -> TaskRecord:
        task.state = TaskState.SUCCEEDED
        task.result = result
        task.finished_at = utcnow()
        return await self._repo.save(task)

    async def fail(
        self,
        task: TaskRecord,
        error: ApplyuminatiError,
        *,
        strategy: str | None = None,
        available_strategies: list[str] | None = None,
    ) -> TaskRecord:
        """Record a failure and apply the recovery policy."""
        attempt = TaskAttempt(
            attempt=task.attempt_count + 1,
            finished_at=utcnow(),
            succeeded=False,
            strategy=strategy,
            failure_category=error.category,
            failure_code=error.code,
            failure_message=error.message,
            recovery=error.recovery,
            details=error.details,
        )
        task.attempts.append(attempt)
        if strategy and strategy not in task.attempted_strategies:
            task.attempted_strategies.append(strategy)

        decision = decide(
            task,
            error,
            available_strategies=available_strategies or [],
        )

        if decision.is_terminal:
            task.state = TaskState.FAILED
            task.finished_at = utcnow()
            task.failure_category = error.category
            task.failure_message = error.message
        elif decision.action == "blocked":
            task.state = TaskState.BLOCKED
            task.finished_at = utcnow()
            task.failure_category = error.category
            task.failure_message = error.message
        else:
            task.state = TaskState.RETRYING
            task.scheduled_for = utcnow() + (decision.delay or task.next_backoff())
            task.lease_expires_at = None

        return await self._repo.save(task)

    async def block(self, task: TaskRecord, message: str) -> TaskRecord:
        """Mark a task as blocked — only a human can advance it."""
        task.state = TaskState.BLOCKED
        task.failure_message = message
        task.finished_at = utcnow()
        return await self._repo.save(task)

    async def reclaim_expired(self) -> int:
        return await self._repo.reclaim_expired_leases()
