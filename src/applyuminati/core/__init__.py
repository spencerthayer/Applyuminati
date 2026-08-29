"""Vendor-neutral core: domain models, provenance, settings, errors, registry.

Nothing in this package may import a database driver, an HTTP client, a web
framework, an LLM SDK, or a browser automation library. That rule is enforced
by the ``import-linter`` contract "Core domain is vendor-neutral" in
``pyproject.toml`` and checked in CI.
"""

from applyuminati.core.clock import ensure_utc, utcnow
from applyuminati.core.errors import (
    ApplyuminatiError,
    FailureCategory,
    RecoveryHint,
)
from applyuminati.core.ids import new_ulid, stable_id
from applyuminati.core.provenance import (
    AssertionLevel,
    Claim,
    EvidenceLink,
    Provenance,
    ProvenanceKind,
)
from applyuminati.core.registry import (
    HealthReport,
    HealthState,
    PluginDescriptor,
    Registry,
)
from applyuminati.core.settings import ExecutionMode, Settings, get_settings
from applyuminati.core.strategy import SearchStrategy

__all__ = [
    "ApplyuminatiError",
    "AssertionLevel",
    "Claim",
    "EvidenceLink",
    "ExecutionMode",
    "FailureCategory",
    "HealthReport",
    "HealthState",
    "PluginDescriptor",
    "Provenance",
    "ProvenanceKind",
    "RecoveryHint",
    "Registry",
    "SearchStrategy",
    "Settings",
    "ensure_utc",
    "get_settings",
    "new_ulid",
    "stable_id",
    "utcnow",
]
