"""Agent backend selection, gated on settings.agents.enabled."""

from __future__ import annotations

import asyncio

from applyuminati.agents.base import AGENT_REGISTRY, AgentBackend
from applyuminati.core.errors import BackendUnavailableError
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import Settings

__all__ = ["probe_all", "select_agent"]


async def select_agent(settings: Settings) -> tuple[AgentBackend, HealthReport] | None:
    """Return the first available agent backend, or ``None`` when disabled."""
    if not settings.agents.enabled:
        return None
    rejections: list[str] = []
    for slug in settings.agents.preferred:
        descriptor = AGENT_REGISTRY.try_get(slug)
        if descriptor is None:
            rejections.append(f"{slug}: not registered")
            continue
        try:
            backend = descriptor.create(settings=settings)
            report = await backend.health()
        except Exception as exc:  # noqa: BLE001
            rejections.append(f"{slug}: {exc}")
            continue
        if report.usable:
            return backend, report
        rejections.append(f"{slug}: {report.state.value} — {report.detail}")
    raise BackendUnavailableError(
        "no agent backend available; rejections: " + "; ".join(rejections),
        code="agent.none_available",
        details={"rejections": rejections, "preferred": list(settings.agents.preferred)},
    )


async def probe_all(settings: Settings) -> list[HealthReport]:
    async def probe(slug: str) -> HealthReport:
        descriptor = AGENT_REGISTRY.try_get(slug)
        if descriptor is None:
            return HealthReport(plugin=slug, state=HealthState.NOT_INSTALLED, detail="not registered")
        try:
            backend = descriptor.create(settings=settings)
            return await backend.health()
        except Exception as exc:  # noqa: BLE001
            return HealthReport(plugin=slug, state=HealthState.UNAVAILABLE, detail=str(exc))

    return list(await asyncio.gather(*(probe(slug) for slug in AGENT_REGISTRY.slugs())))
