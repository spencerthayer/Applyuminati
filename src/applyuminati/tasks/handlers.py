"""Task handler machinery: the contract between the queue and real work.

A handler is data, not a subclass: a kind, a Pydantic schema for its payload,
an async callable, and the ordered list of strategies it knows how to try. That
shape is what makes the queue's self-healing possible — the recovery policy can
ask "what other approach exists for this kind of work?" without knowing what
the work *is*.

This module defines the machinery only. The job-specific handlers (fetch a
source, score a job, run an application) are registered by the services layer,
which is the only layer allowed to depend on every capability at once.

The payload schema is deliberately mandatory. A durable queue that accepts
free-form dicts turns a typo into a crash loop three days later; validating at
claim time turns it into one clearly-labelled ``CONFIGURATION`` failure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import structlog
from pydantic import BaseModel

from applyuminati.core.errors import ConfigurationError

PayloadT = TypeVar("PayloadT", bound=BaseModel)

#: Persists partial progress for the running task. Supplied by the worker.
CheckpointSink = Callable[[dict[str, Any]], Awaitable[None]]

#: What a handler implementation looks like.
HandlerFn = Callable[[PayloadT, "TaskContext"], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class TaskContext:
    """Everything a handler needs about *this* execution of its task.

    ``resume_state`` is the same dict the previous attempt checkpointed, so a
    handler resumes from a cursor rather than restarting. ``strategy`` is the
    approach the recovery policy selected for this attempt; a handler that only
    knows one way of working may ignore it.
    """

    task_id: str
    kind: str
    run_id: str | None
    #: 1-based index of this attempt.
    attempt: int
    #: Approach chosen for this attempt, or ``None`` when the kind has only one.
    strategy: str | None
    #: Mutable partial progress. Mutate through :meth:`checkpoint`, not directly.
    resume_state: dict[str, Any]
    logger: structlog.stdlib.BoundLogger
    #: Writes ``resume_state`` through to storage. Injected by the worker.
    checkpoint_sink: CheckpointSink = field(repr=False)

    async def checkpoint(self, **state: Any) -> None:
        """Merge ``state`` into ``resume_state`` and persist it immediately.

        Called at every point where losing progress would be expensive — after
        a page of results, after a form section is filled. The merge is
        shallow: a handler that needs nested progress should checkpoint a whole
        sub-dict under one key.
        """
        self.resume_state.update(state)
        await self.checkpoint_sink(dict(self.resume_state))


@dataclass(frozen=True, slots=True)
class TaskHandler(Generic[PayloadT]):
    """Static registration data for one task kind."""

    kind: str
    input_schema: type[PayloadT]
    run: HandlerFn[PayloadT]
    #: Ordered, cheapest-and-most-reliable first. The recovery policy walks
    #: this list on drift, never repeating an entry.
    strategies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.strategies)) != len(self.strategies):
            msg = f"handler {self.kind!r} declares duplicate strategies: {self.strategies}"
            raise ValueError(msg)


#: Process-wide handler table. Populated by the services layer at startup.
HANDLER_REGISTRY: dict[str, TaskHandler[Any]] = {}


def register_handler(
    kind: str,
    input_schema: type[PayloadT],
    *,
    strategies: Sequence[str] = (),
    registry: dict[str, TaskHandler[Any]] | None = None,
) -> Callable[[HandlerFn[PayloadT]], HandlerFn[PayloadT]]:
    """Register a task handler and return the function unchanged.

    Returning the original callable keeps the decorated function directly
    testable and callable by name, which matters because handlers are ordinary
    functions that happen to be reachable from the queue.
    """
    target = HANDLER_REGISTRY if registry is None else registry

    def decorate(fn: HandlerFn[PayloadT]) -> HandlerFn[PayloadT]:
        if kind in target:
            msg = (
                f"task kind {kind!r} is already registered to "
                f"{getattr(target[kind].run, '__qualname__', '<unknown>')}"
            )
            raise ConfigurationError(msg, code="tasks.duplicate_handler")
        target[kind] = TaskHandler(
            kind=kind,
            input_schema=input_schema,
            run=fn,
            strategies=tuple(strategies),
        )
        return fn

    return decorate


def get_handler(
    kind: str,
    *,
    registry: dict[str, TaskHandler[Any]] | None = None,
) -> TaskHandler[Any]:
    """Return the handler for ``kind``, or raise :class:`ConfigurationError`.

    An unknown kind is a configuration failure rather than a lookup miss: a
    durable row exists naming work nothing can perform, and that needs to be
    visible, not retried.
    """
    target = HANDLER_REGISTRY if registry is None else registry
    try:
        return target[kind]
    except KeyError as exc:
        known = ", ".join(sorted(target)) or "none"
        msg = f"no handler registered for task kind {kind!r} (registered: {known})"
        raise ConfigurationError(msg, code="tasks.unknown_kind") from exc


def strategies_for(
    kind: str,
    *,
    registry: dict[str, TaskHandler[Any]] | None = None,
) -> tuple[str, ...]:
    """Return the strategies registered for ``kind``, empty if it is unknown.

    Unlike :func:`get_handler` this never raises: the recovery policy asks for
    alternatives while handling a failure, and a second failure there would
    hide the first.
    """
    target = HANDLER_REGISTRY if registry is None else registry
    handler = target.get(kind)
    return handler.strategies if handler is not None else ()


def unregister_handler(
    kind: str,
    *,
    registry: dict[str, TaskHandler[Any]] | None = None,
) -> None:
    """Remove ``kind`` from the registry. Used by tests and by hot reload."""
    target = HANDLER_REGISTRY if registry is None else registry
    target.pop(kind, None)


__all__ = [
    "HANDLER_REGISTRY",
    "CheckpointSink",
    "HandlerFn",
    "TaskContext",
    "TaskHandler",
    "get_handler",
    "register_handler",
    "strategies_for",
    "unregister_handler",
]
