"""Browser automation contract and backend selection.

Re-exports the contract types. Concrete backends live in
:mod:`applyuminati.plugins.browsers`; this package never imports them.
"""

from applyuminati.browser.base import (
    BROWSER_REGISTRY,
    ActionResult,
    BrowserBackend,
    BrowserCapability,
    BrowserCheckpoint,
    BrowserMetadata,
    BrowserSession,
    ControlOwner,
    ElementRole,
    HANDOFF_CONDITIONS,
    PageCondition,
    PageElement,
    PageObservation,
    browser_plugin,
)
from applyuminati.browser.selection import probe_all, select_browser

__all__ = [
    "BROWSER_REGISTRY",
    "HANDOFF_CONDITIONS",
    "ActionResult",
    "BrowserBackend",
    "BrowserCapability",
    "BrowserCheckpoint",
    "BrowserMetadata",
    "BrowserSession",
    "ControlOwner",
    "ElementRole",
    "PageCondition",
    "PageElement",
    "PageObservation",
    "browser_plugin",
    "probe_all",
    "select_browser",
]
