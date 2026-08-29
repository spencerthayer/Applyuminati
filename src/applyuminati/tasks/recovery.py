"""Self-healing policy: what to do after a task attempt fails.

This module is the single place where "should we try again, try differently,
give up, or ask the human?" is decided. It is deliberately **pure**: it takes a
:class:`~applyuminati.core.models.task.TaskRecord` plus the
:class:`~applyuminati.core.errors.ApplyuminatiError` that ended the attempt and
returns a :class:`RecoveryDecision`. No I/O and no clock reads beyond the
task's own backoff helper, so the policy is exhaustively testable.

Two distinctions carry most of the weight:

``BLOCKED`` vs ``FAILED``
    ``BLOCKED`` means *this exact task can still succeed once a human acts* —
    supply credentials, solve a challenge, make a judgement call. ``FAILED``
    means the task as submitted can never succeed and a human must submit
    corrected work instead. Auth and human-challenge failures therefore never
    retry and never "fail"; they park.

Attempt budget vs strategy budget
    A task has two independent exhaustion conditions: ``max_attempts`` (how
    many times we are willing to run it at all) and the set of registered
    strategies for its kind (how many *different* ways we know how to do it).
    Either running out ends the task, so a drifting scraper can never spin
    forever.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from applyuminati.core.errors import ApplyuminatiError, FailureCategory, RecoveryHint
from applyuminati.core.models.task import TaskRecord, TaskState

_ZERO = timedelta(0)

#: The only states :func:`decide` may return. Anything else would mean the
#: policy is trying to do the queue's job.
DECIDABLE_STATES: frozenset[TaskState] = frozenset(
    {TaskState.RETRYING, TaskState.FAILED, TaskState.BLOCKED}
)

#: Failures where only a human can move the task forward. Never retried and
#: never marked failed: the task stays parked and resumable.
BLOCKING_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.AUTH_REQUIRED,
        FailureCategory.HUMAN_CHALLENGE,
        FailureCategory.NEEDS_HUMAN,
    }
)

#: Failures where repeating the work — with any strategy — cannot help.
TERMINAL_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.RESOURCE_GONE,
        FailureCategory.DUPLICATE_ACTION,
        FailureCategory.POLICY_REFUSED,
        FailureCategory.STORAGE,
        # A malformed payload or a missing handler is not something a retry or
        # a human unblock fixes; the caller must enqueue corrected work.
        FailureCategory.CONFIGURATION,
    }
)

#: Failures that mean "this *approach* stopped working", so try another one.
STRATEGY_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.EXTRACTION_DRIFT,
        FailureCategory.AUTOMATION_BLOCKED,
        FailureCategory.ENDPOINT_UNAVAILABLE,
        FailureCategory.BACKEND_UNAVAILABLE,
    }
)

#: A model that produced unparseable output twice will not produce parseable
#: output from a third identical prompt.
MAX_INVALID_OUTPUT_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """What the queue should do with a task that just failed."""

    #: One of :data:`DECIDABLE_STATES`.
    action: TaskState
    #: How long to wait before the next attempt. Zero for terminal actions.
    delay: timedelta
    #: Strategy to use on the next attempt, when switching approach.
    next_strategy: str | None
    #: Human-readable justification, recorded on the attempt for auditability.
    reason: str

    def __post_init__(self) -> None:
        if self.action not in DECIDABLE_STATES:
            msg = f"RecoveryDecision.action must be one of {sorted(DECIDABLE_STATES)}"
            raise ValueError(msg)

    @property
    def is_terminal(self) -> bool:
        """True when no further automated attempt will be made."""
        return self.action is not TaskState.RETRYING


def next_strategy(available: Iterable[str], attempted: Iterable[str]) -> str | None:
    """Return the first strategy in ``available`` that is not in ``attempted``.

    Registration order is the preference order: a source lists its cheapest,
    most reliable strategy first and its expensive fallbacks last. Because the
    queue records every strategy it has burned on a task, this function alone
    guarantees no strategy is ever tried twice.
    """
    burned = set(attempted)
    return next((name for name in available if name not in burned), None)


def decide(
    task: TaskRecord,
    error: ApplyuminatiError,
    *,
    available_strategies: Sequence[str],
) -> RecoveryDecision:
    """Choose the next action for ``task`` after ``error`` ended an attempt.

    ``task`` must already include the failed attempt, and its strategy must
    already be in ``attempted_strategies``: the policy reasons about "how many
    times have we tried this", so counting happens before deciding.
    """
    alternative = next_strategy(available_strategies, task.attempted_strategies)

    hard = _hard_category_rule(error)
    if hard is not None:
        return hard
    guard = _loop_guard(task)
    if guard is not None:
        return guard
    specific = _retry_category_rule(task, error, alternative)
    if specific is not None:
        return specific
    return _hint_rule(task, error, alternative)


def _hard_category_rule(error: ApplyuminatiError) -> RecoveryDecision | None:
    """Rules that hold regardless of how many attempts remain."""
    category = error.category
    if category in BLOCKING_CATEGORIES:
        return RecoveryDecision(
            action=TaskState.BLOCKED,
            delay=_ZERO,
            next_strategy=None,
            reason=f"{category.value}: parked for a human; automated retry cannot resolve it",
        )
    if category in TERMINAL_CATEGORIES:
        return RecoveryDecision(
            action=TaskState.FAILED,
            delay=_ZERO,
            next_strategy=None,
            reason=f"{category.value}: the task as submitted can never succeed",
        )
    return None


def _loop_guard(task: TaskRecord) -> RecoveryDecision | None:
    """Refuse to schedule an attempt beyond the task's declared budget."""
    if task.attempt_count >= task.max_attempts:
        return RecoveryDecision(
            action=TaskState.FAILED,
            delay=_ZERO,
            next_strategy=None,
            reason=f"attempt budget exhausted ({task.attempt_count}/{task.max_attempts} attempts)",
        )
    return None


def _retry_category_rule(
    task: TaskRecord,
    error: ApplyuminatiError,
    alternative: str | None,
) -> RecoveryDecision | None:
    """Category-specific rules for failures that may still make progress."""
    category = error.category
    if category in STRATEGY_CATEGORIES:
        return _strategy_decision(task, category, alternative)
    if category is FailureCategory.INVALID_MODEL_OUTPUT:
        return _invalid_output_decision(task)
    if category is FailureCategory.RATE_LIMITED:
        return RecoveryDecision(
            action=TaskState.RETRYING,
            delay=_rate_limit_delay(task, error),
            next_strategy=None,
            reason="rate_limited: honouring the requested cooldown before retrying",
        )
    return None


def _hint_rule(
    task: TaskRecord,
    error: ApplyuminatiError,
    alternative: str | None,
) -> RecoveryDecision:
    """Fall back to the error's recovery hint (``DEFAULT_RECOVERY`` by default)."""
    hint = error.recovery
    if hint in (RecoveryHint.RETRY, RecoveryHint.RETRY_AFTER_BACKOFF):
        delay = task.next_backoff() if hint is RecoveryHint.RETRY_AFTER_BACKOFF else _ZERO
        return RecoveryDecision(
            action=TaskState.RETRYING,
            delay=delay,
            next_strategy=None,
            reason=f"{error.category.value}: {hint.value}",
        )
    if hint in (RecoveryHint.TRY_ALTERNATIVE_STRATEGY, RecoveryHint.DEGRADE):
        return _strategy_decision(task, error.category, alternative)
    if hint is RecoveryHint.ESCALATE_TO_USER:
        return RecoveryDecision(
            action=TaskState.BLOCKED,
            delay=_ZERO,
            next_strategy=None,
            reason=f"{error.category.value}: escalated to the user",
        )
    return RecoveryDecision(
        action=TaskState.FAILED,
        delay=_ZERO,
        next_strategy=None,
        reason=f"{error.category.value}: aborted",
    )


def _strategy_decision(
    task: TaskRecord,
    category: FailureCategory,
    alternative: str | None,
) -> RecoveryDecision:
    """Switch approach if we know another one, otherwise stop."""
    if alternative is None:
        detail = (
            "no fallback backend is available; degraded to failure"
            if category is FailureCategory.BACKEND_UNAVAILABLE
            else "every registered strategy has been tried"
        )
        burned = ", ".join(task.attempted_strategies) or "none recorded"
        return RecoveryDecision(
            action=TaskState.FAILED,
            delay=_ZERO,
            next_strategy=None,
            reason=f"{category.value}: {detail} (tried: {burned})",
        )
    return RecoveryDecision(
        action=TaskState.RETRYING,
        delay=task.next_backoff(),
        next_strategy=alternative,
        reason=f"{category.value}: switching to strategy {alternative!r}",
    )


def _invalid_output_decision(task: TaskRecord) -> RecoveryDecision:
    """Re-prompt a small, fixed number of times, then stop."""
    if task.attempt_count >= MAX_INVALID_OUTPUT_ATTEMPTS:
        return RecoveryDecision(
            action=TaskState.FAILED,
            delay=_ZERO,
            next_strategy=None,
            reason=(
                "invalid_model_output: schema validation failed on "
                f"{task.attempt_count} attempts; not re-prompting again"
            ),
        )
    return RecoveryDecision(
        action=TaskState.RETRYING,
        delay=task.next_backoff(),
        next_strategy=None,
        reason="invalid_model_output: re-prompting with the same strategy",
    )


def _rate_limit_delay(task: TaskRecord, error: ApplyuminatiError) -> timedelta:
    """Respect an explicit ``Retry-After`` but never wait less than our backoff."""
    backoff = task.next_backoff()
    if error.retry_after_seconds is None:
        return backoff
    return max(backoff, timedelta(seconds=error.retry_after_seconds))


__all__ = [
    "BLOCKING_CATEGORIES",
    "DECIDABLE_STATES",
    "MAX_INVALID_OUTPUT_ATTEMPTS",
    "STRATEGY_CATEGORIES",
    "TERMINAL_CATEGORIES",
    "RecoveryDecision",
    "decide",
    "next_strategy",
]
