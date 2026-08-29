"""Structured logging.

Every log line is a structured event. Runs and tasks bind stable IDs into a
context variable so that a discovery run, its per-source stages, and any
failures can be reconstructed later — which is the raw material future
self-healing logic needs.

A redaction processor runs last in the chain, so nothing added by application
code can bypass it.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from applyuminati.core.redaction import redact_value
from applyuminati.core.settings import LogFormat

_configured = False


def _redaction_processor(
    _logger: Any,  # noqa: ANN401
    _name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    return {key: redact_value(value) for key, value in event_dict.items()}


def configure_logging(*, level: str = "INFO", fmt: LogFormat = LogFormat.CONSOLE) -> None:
    """Configure structlog and the stdlib root logger. Idempotent."""
    global _configured  # noqa: PLW0603
    numeric = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt is LogFormat.JSON
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redaction_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=numeric, stream=sys.stderr, format="%(message)s", force=True)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, configuring defaults on first use."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]


@contextmanager
def bound_context(**values: Any) -> Iterator[None]:  # noqa: ANN401
    """Bind ``values`` (run_id, task_id, source, stage…) for the enclosed block."""
    tokens = structlog.contextvars.bind_contextvars(**values)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


__all__ = ["bound_context", "configure_logging", "get_logger"]
