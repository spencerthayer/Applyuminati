"""First-party job-source plugins.

Registration is a function, not an import side effect: importing this package
must not pull ``httpx`` or open any client. The services layer calls
:func:`register_sources` once at startup.
"""

from __future__ import annotations

from applyuminati.sources.base import SOURCE_REGISTRY


def register_sources() -> None:
    """Register the built-in job-source plugins. Idempotent."""
    if "greenhouse" not in SOURCE_REGISTRY:
        from applyuminati.plugins.sources.greenhouse import PLUGIN as greenhouse

        SOURCE_REGISTRY.register(greenhouse)
    if "lever" not in SOURCE_REGISTRY:
        from applyuminati.plugins.sources.lever import PLUGIN as lever

        SOURCE_REGISTRY.register(lever)
    if "local_feed" not in SOURCE_REGISTRY:
        from applyuminati.plugins.sources.local_feed import PLUGIN as local_feed

        SOURCE_REGISTRY.register(local_feed)


__all__ = ["register_sources"]
