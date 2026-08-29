"""Time access.

Every timestamp in Applyuminati is timezone-aware UTC. Domain code calls
:func:`utcnow` rather than ``datetime.now()`` so tests can freeze time by
swapping the module-level clock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def _system_clock() -> datetime:
    return datetime.now(UTC)


_clock: Clock = _system_clock


def utcnow() -> datetime:
    """Return the current timezone-aware UTC time."""
    return _clock()


def set_clock(clock: Clock) -> Clock:
    """Install ``clock`` as the process clock and return the previous one."""
    global _clock  # noqa: PLW0603 - single deliberate process-wide seam
    previous = _clock
    _clock = clock
    return previous


def reset_clock() -> None:
    """Restore the system clock."""
    set_clock(_system_clock)


def ensure_utc(value: datetime) -> datetime:
    """Coerce ``value`` to timezone-aware UTC.

    Naive datetimes are *assumed* to be UTC rather than rejected, because
    third-party feeds routinely emit naive ISO-8601 strings.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
