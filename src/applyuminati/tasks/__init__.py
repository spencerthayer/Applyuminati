"""Durable tasks: queue, worker, handlers, recovery policy.

The queue is SQLite-backed and lease-based, so a crashed worker's tasks are
reclaimed rather than lost. The recovery policy in :mod:`tasks.recovery` is
the self-healing decision point: it maps failure categories to retry,
fallback, block or abort, with a hard loop guard.
"""

from applyuminati.tasks.handlers import TaskContext, TaskHandler, get_handler, register_handler
from applyuminati.tasks.queue import TaskQueue
from applyuminati.tasks.recovery import RecoveryDecision, decide
from applyuminati.tasks.worker import TaskWorker

__all__ = [
    "RecoveryDecision",
    "TaskContext",
    "TaskHandler",
    "TaskQueue",
    "TaskWorker",
    "decide",
    "get_handler",
    "register_handler",
]
