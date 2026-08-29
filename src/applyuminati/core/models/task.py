"""Durable tasks and runs.

Long work (discovery, scoring, application execution) is expressed as
**typed tasks with observable state**, not as fire-and-forget coroutines. A
task row survives process restart, records every attempt with its failure
classification, and carries a ``resume_state`` blob so a partially-completed
stage can continue instead of starting over.

This is the domain model only. The SQLite-backed runner lives in
:mod:`applyuminati.tasks`; replacing it with a real queue later means
implementing the same interface, not changing these types.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.errors import FailureCategory, RecoveryHint
from applyuminati.core.ids import new_ulid


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    #: Failed but eligible for another attempt.
    RETRYING = "retrying"
    #: Failed terminally; inspectable, manually re-runnable.
    FAILED = "failed"
    #: Stopped because only a human can proceed.
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TASK_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
)


class RunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    #: Some tasks failed; the run completed and the failures are recorded.
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskAttempt(BaseModel):
    """One execution attempt, successful or not.

    Kept per-attempt (rather than as a counter) because *which strategy was
    tried* is the information self-healing needs.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    attempt: int = Field(ge=1)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    succeeded: bool = False
    #: Named strategy used, e.g. ``greenhouse.board_api`` or ``browser.ego_lite``.
    strategy: str | None = None
    failure_category: FailureCategory | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    recovery: RecoveryHint | None = None
    #: Redaction-safe structured detail.
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class TaskRecord(BaseModel):
    """A unit of durable work."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    run_id: str | None = None
    #: Registered handler name, e.g. ``discovery.fetch_source``.
    kind: str
    state: TaskState = TaskState.PENDING
    #: Validated by the handler's input schema before execution.
    payload: dict[str, Any] = Field(default_factory=dict)
    #: Handler output, once it succeeds.
    result: dict[str, Any] | None = None
    #: Enough state to continue a partially-completed task (cursor, page, step).
    resume_state: dict[str, Any] = Field(default_factory=dict)

    #: Deduplication key. Two pending tasks with the same key are the same work.
    idempotency_key: str | None = None
    priority: int = 0
    max_attempts: int = 3
    attempts: list[TaskAttempt] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    scheduled_for: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Set when a running task's lease expires, so a crashed worker is recoverable.
    lease_expires_at: datetime | None = None

    #: Populated when the task ends in FAILED/BLOCKED, mirroring the last attempt.
    failure_category: FailureCategory | None = None
    failure_message: str | None = None
    #: Strategies already tried, so an alternative can be chosen next.
    attempted_strategies: list[str] = Field(default_factory=list)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def is_terminal(self) -> bool:
        return self.state in TASK_TERMINAL_STATES

    @property
    def can_retry(self) -> bool:
        return self.attempt_count < self.max_attempts and not self.is_terminal

    def next_backoff(self, *, base_seconds: float = 2.0, cap_seconds: float = 300.0) -> timedelta:
        """Exponential backoff for the next attempt, capped.

        Deterministic (no jitter) because a single local process is not a
        thundering herd, and reproducible timings make tests honest.
        """
        delay = min(cap_seconds, base_seconds * (2 ** max(0, self.attempt_count - 1)))
        return timedelta(seconds=delay)

    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class RunRecord(BaseModel):
    """A user-visible operation composed of tasks (a discovery run, a scoring pass)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    kind: str
    state: RunState = RunState.RUNNING
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    #: Redaction-safe parameters the run was started with.
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: Rolling counters: jobs discovered, duplicates merged, tasks failed…
    stats: dict[str, int] = Field(default_factory=dict)
    #: One line per failure, so a partial run explains itself.
    failures: list[str] = Field(default_factory=list)
    #: Who started it: ``cli``, ``api``, ``schedule``.
    triggered_by: str = "cli"

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def bump(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount


__all__ = [
    "TASK_TERMINAL_STATES",
    "RunRecord",
    "RunState",
    "TaskAttempt",
    "TaskRecord",
    "TaskState",
]
