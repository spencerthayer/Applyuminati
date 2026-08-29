"""First-party agent runtime adapters."""

from __future__ import annotations

from applyuminati.agents.base import AGENT_REGISTRY


def register_agents() -> None:
    """Register built-in agent backends. Idempotent."""
    from applyuminati.plugins.agents.cli_agents import (
        CLAUDE_CODE,
        CODEX,
        OH_MY_PI,
        OPENCODE,
        PI,
    )

    for descriptor in (OH_MY_PI, CODEX, CLAUDE_CODE, OPENCODE, PI):
        if descriptor.slug not in AGENT_REGISTRY:
            AGENT_REGISTRY.register(descriptor)


__all__ = ["register_agents"]
