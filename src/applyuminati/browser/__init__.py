"""Browser automation contract, capabilities and backend selection.

Re-exports the contract types. Concrete backends live in
:mod:`applyuminati.plugins.browsers`; this package never imports them.
"""

from applyuminati.browser.base import (
    BROWSER_REGISTRY,
    HANDOFF_CONDITIONS,
    ActionResult,
    BrowserBackend,
    BrowserCapability,
    BrowserCheckpoint,
    BrowserMetadata,
    BrowserSession,
    ControlOwner,
    ElementRole,
    PageCondition,
    PageElement,
    PageObservation,
    browser_plugin,
)
from applyuminati.browser.capabilities import (
    APPLICATION_SUBMISSION,
    AUTHENTICATED_APPLICATION,
    READ_ONLY_INSPECTION,
    BackendCandidate,
    BrowserRequirements,
    CapabilityMaturity,
    capability_matrix,
)
from applyuminati.browser.selection import evaluate_backends, probe_all, select_browser

__all__ = [
    "APPLICATION_SUBMISSION",
    "AUTHENTICATED_APPLICATION",
    "BROWSER_REGISTRY",
    "HANDOFF_CONDITIONS",
    "READ_ONLY_INSPECTION",
    "ActionResult",
    "BackendCandidate",
    "BrowserBackend",
    "BrowserCapability",
    "BrowserCheckpoint",
    "BrowserMetadata",
    "BrowserRequirements",
    "BrowserSession",
    "CapabilityMaturity",
    "ControlOwner",
    "ElementRole",
    "PageCondition",
    "PageElement",
    "PageObservation",
    "browser_plugin",
    "capability_matrix",
    "evaluate_backends",
    "probe_all",
    "select_browser",
]
