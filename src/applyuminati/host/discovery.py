"""Local backend advertisement for the Browser Host.

The host reports what it can actually run. ego lite is preferred when the
helper is present; Playwright is advertised when the extra is installed.
Availability is probed, not assumed from the platform string.
"""

from __future__ import annotations

from applyuminati.browser.host_protocol import BackendAdvertisement
from applyuminati.core.platform import current_platform
from applyuminati.core.registry import HealthState
from applyuminati.core.settings import Settings
from applyuminati.host.dispatcher import host_advertised_capabilities

__all__ = ["advertise_backends", "loopback_url"]


async def advertise_backends(settings: Settings) -> dict[str, BackendAdvertisement]:
    """Probe registered backends and describe them for the register frame."""
    from applyuminati.browser.base import BROWSER_REGISTRY
    from applyuminati.plugins.browsers import register_browsers

    register_browsers()
    advertised: dict[str, BackendAdvertisement] = {}
    platform = current_platform()
    for descriptor in BROWSER_REGISTRY.all():
        try:
            backend = descriptor.create(settings=settings)
        except TypeError:
            backend = descriptor.create()
        metadata = backend.metadata
        if platform not in metadata.platforms:
            advertised[descriptor.slug] = BackendAdvertisement(
                available=False,
                preferred=descriptor.slug == "ego_lite",
                capabilities=host_advertised_capabilities(metadata.capabilities),
                detail=f"{descriptor.slug} does not run on {platform}",
            )
            continue
        health = await backend.health()
        available = health.state in (HealthState.HEALTHY, HealthState.DEGRADED)
        advertised[descriptor.slug] = BackendAdvertisement(
            available=available,
            preferred=descriptor.slug == "ego_lite" and available,
            version=str(health.facts.get("version")) if health.facts.get("version") else None,
            capabilities=host_advertised_capabilities(metadata.capabilities),
            detail=health.detail or None,
        )
        await backend.aclose()
    return advertised


def loopback_url(port: int = 8000) -> str:
    return f"ws://127.0.0.1:{port}/api/v1/browser-hosts/ws"
