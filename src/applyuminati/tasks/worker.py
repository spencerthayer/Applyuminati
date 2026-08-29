"""In-process async task worker.

Claims one task at a time, validates the payload against the handler's input
schema, executes with a timeout, and records the outcome. Crashed workers are
detected by lease expiry and their tasks are reclaimed.

The worker is deliberately simple — no prefetch, no concurrency within one
worker — because a single local process is not a thundering herd. The
interface (claim, execute, complete/fail) is what a real queue would replace.
"""

from __future__ import annotations

import asyncio
from typing import Any

from applyuminati.core.errors import ApplyuminatiError, ConfigurationError, FailureCategory
from applyuminati.core.logging import bound_context, get_logger
from applyuminati.core.models.task import TaskRecord, TaskState
from applyuminati.tasks.handlers import TaskContext, get_handler
from applyuminati.tasks.queue import TaskQueue

log = get_logger(__name__)

__all__ = ["TaskWorker"]


class TaskWorker:
    def __init__(self, queue: TaskQueue, *, checkpoint_sink: Any | None = None) -> None:
        self._queue = queue
        self._checkpoint_sink = checkpoint_sink

    async def run_once(self, *, kinds: list[str] | None = None) -> bool:
        """Claim and execute one task. Returns whether work was done."""
        await self._queue.reclaim_expired()
        task = await self._queue.claim(kinds=kinds)
        if task is None:
            return False

        with bound_context(run_id=task.run_id, task_id=task.id, kind=task.kind):
            await self._execute(task)
        return True

    async def run_forever(
        self,
        *,
        poll_interval: float = 1.0,
        kinds: list[str] | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Poll and execute until ``stop_event`` is set or cancelled."""
        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            did_work = await self.run_once(kinds=kinds)
            if not did_work:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass

    async def _execute(self, task: TaskRecord) -> None:
        handler = get_handler(task.kind)
        if handler is None:
            log.error("worker.no_handler", task_id=task.id, kind=task.kind)
            task.state = TaskState.FAILED
            task.failure_category = FailureCategory.CONFIGURATION
            task.failure_message = f"no handler registered for kind {task.kind!r}"
            task.finished_at = applyuminati_core_clock_utcnow()
            await self._queue._repo.save(task)
            return

        # Validate the payload against the handler's input schema.
        try:
            validated = handler.input_schema.model_validate(task.payload)
        except Exception as exc:
            log.error("worker.invalid_payload", task_id=task.id, kind=task.kind, error=str(exc))
            task.state = TaskState.FAILED
            task.failure_category = FailureCategory.CONFIGURATION
            task.failure_message = f"invalid payload: {exc}"
            task.finished_at = _utcnow()
            await self._queue._repo.save(task)
            return

        context = TaskContext(
            run_id=task.run_id,
            task_id=task.id,
            resume_state=dict(task.resume_state),
        )
        started = _utcnow()
        try:
            result = await asyncio.wait_for(
                handler.run(validated, context),
                timeout=handler.timeout_seconds if hasattr(handler, "timeout_seconds") else 600.0,
            )
            await self._queue.complete(task, result)
            log.info(
                "worker.task_succeeded",
                task_id=task.id,
                kind=task.kind,
                duration_s=(_utcnow() - started).total_seconds(),
            )
        except ApplyuminatiError as exc:
            await self._queue.fail(
                task,
                exc,
                strategy=handler.strategies[0] if handler.strategies else None,
                available_strategies=list(handler.strategies),
            )
            log.warning(
                "worker.task_failed",
                task_id=task.id,
                kind=task.kind,
                error=exc.code,
                category=exc.category.value,
            )
        except asyncio.TimeoutError:
            from applyuminati.core.errors import TransientNetworkError

            await self._queue.fail(
                task,
                TransientNetworkError(
                    f"task timed out after {getattr(handler, 'timeout_seconds', 600)}s",
                    code="worker.timeout",
                ),
                strategy=handler.strategies[0] if handler.strategies else None,
                available_strategies=list(handler.strategies),
            )
            log.warning("worker.task_timeout", task_id=task.id, kind=task.kind)
        except Exception as exc:
            from applyuminati.core.errors import ApplyuminatiError as _AE

            class _UnexpectedError(_AE):
                category = FailureCategory.UNKNOWN

            await self._queue.fail(
                task,
                _UnexpectedError(f"{type(exc).__name__}: {exc}", code="worker.unexpected"),
                strategy=handler.strategies[0] if handler.strategies else None,
                available_strategies=list(handler.strategies),
            )
            log.exception("worker.task_crashed", task_id=task.id, kind=task.kind)


def _utcnow():
    from applyuminati.core.clock import utcnow

    return utcnow()
