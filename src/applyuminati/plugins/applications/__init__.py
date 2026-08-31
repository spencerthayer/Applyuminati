"""First-party ApplicationDriver implementations."""

from __future__ import annotations


def register_application_drivers() -> None:
    """Register built-in application drivers. Idempotent."""
    from applyuminati.applications.driver import APPLICATION_DRIVER_REGISTRY

    if "greenhouse" not in APPLICATION_DRIVER_REGISTRY:
        from applyuminati.plugins.applications.greenhouse import PLUGIN as greenhouse

        APPLICATION_DRIVER_REGISTRY.register(greenhouse)
    if "lever" not in APPLICATION_DRIVER_REGISTRY:
        from applyuminati.plugins.applications.lever import PLUGIN as lever

        APPLICATION_DRIVER_REGISTRY.register(lever)


__all__ = ["register_application_drivers"]
