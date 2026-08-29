"""Browser backend selection: first available wins.

Walks ``settings.browser.preferred`` in order, probes health, and returns the
first usable backend. A host without ego lite silently falls back to
Playwright; a host with neither gets a clear error listing every backend and
why it was rejected.
"""

from __future__ import annotations

import asyncio

from applyuminati.browser.base import BROWSER_REGISTRY, BrowserBackend
from applyuminati.core.errors import BackendUnavailableError
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import Settings

__all__ = ["probe_all", "select_browser"]


async def select_browser(settings: Settings) -> tuple[BrowserBackend, HealthReport]:
    """Return the first available browser backend and its health report."""
    rejections: list[str] = []
    for slug in settings.browser.preferred:
        descriptor = BROWSER_REGISTRY.try_get(slug)
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
        "no browser backend available; rejections: " + "; ".join(rejections),
        code="browser.none_available",
        details={"rejections": rejections, "preferred": list(settings.browser.preferred)},
    )


async def probe_all(settings: Settings) -> list[HealthReport]:
    """Probe every registered browser backend concurrently."""
    async def probe(slug: str) -> HealthReport:
        descriptor = BROWSER_REGISTRY.try_get(slug)
        if descriptor is None:
            return HealthReport(plugin=slug, state=HealthState.NOT_INSTALLED, detail="not registered")
        try:
            backend = descriptor.create(settings=settings)
            return await backend.health()
        except Exception as exc:  # noqa: BLE001
            return HealthReport(plugin=slug, state=HealthState.UNAVAILABLE, detail=str(exc))

    return list(await asyncio.gather(*(probe(slug) for slug in BROWSER_REGISTRY.slugs())))
