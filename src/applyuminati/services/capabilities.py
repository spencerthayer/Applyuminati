"""Generated capability matrix.

Documentation and ``applyuminati capabilities`` read this function. A
hand-maintained table would drift from the registries the first time a
plugin is added or a maturity claim changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from applyuminati.agents.base import AGENT_REGISTRY
from applyuminati.applications.driver import APPLICATION_DRIVER_REGISTRY
from applyuminati.browser.base import BROWSER_REGISTRY
from applyuminati.core.registry import PluginMaturity, Registry
from applyuminati.email.base import EMAIL_REGISTRY
from applyuminati.llm.base import LLM_REGISTRY
from applyuminati.services.container import _register_builtin_plugins
from applyuminati.sources.base import SOURCE_REGISTRY

__all__ = ["CapabilityRow", "collect_capability_matrix"]


@dataclass(frozen=True, slots=True)
class CapabilityRow:
    kind: str
    slug: str
    name: str
    maturity: PluginMaturity
    capabilities: tuple[str, ...]
    origin: str


def collect_capability_matrix() -> list[CapabilityRow]:
    """Every registered plugin, with the maturity it actually declares."""
    _register_builtin_plugins()
    rows: list[CapabilityRow] = []
    for registry in (
        SOURCE_REGISTRY,
        BROWSER_REGISTRY,
        AGENT_REGISTRY,
        EMAIL_REGISTRY,
        LLM_REGISTRY,
        APPLICATION_DRIVER_REGISTRY,
    ):
        rows.extend(_rows_from(registry))
    return sorted(rows, key=lambda row: (row.kind, row.slug))


def _rows_from(registry: Registry[Any]) -> list[CapabilityRow]:
    return [
        CapabilityRow(
            kind=descriptor.kind,
            slug=descriptor.slug,
            name=descriptor.name,
            maturity=descriptor.maturity,
            capabilities=tuple(sorted(descriptor.capabilities)),
            origin=descriptor.origin,
        )
        for descriptor in registry.all()
    ]
