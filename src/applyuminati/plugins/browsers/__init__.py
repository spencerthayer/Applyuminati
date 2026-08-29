"""First-party browser backends.

Registration is a function, not an import side effect: importing this package
must not pull Playwright or attempt to detect ego lite. Shared page-analysis
helpers are re-exported from :mod:`applyuminati.plugins.browsers.shared` so
the ego lite and Playwright adapters can use them without duplicating the
detection logic.
"""

from __future__ import annotations

from applyuminati.plugins.browsers.shared import (
    CONTROL_SCAN_CALL_LITERAL,
    MAX_TEXT_CHARS,
    detect_condition,
    parse_scanned_controls,
    split_locator,
)


def register_browsers() -> None:
    """Register built-in browser backends. Idempotent."""
    from applyuminati.browser.base import BROWSER_REGISTRY

    if "ego_lite" not in BROWSER_REGISTRY:
        from applyuminati.plugins.browsers.ego_lite import PLUGIN as ego_lite

        BROWSER_REGISTRY.register(ego_lite)
    if "playwright" not in BROWSER_REGISTRY:
        from applyuminati.plugins.browsers.playwright_backend import PLUGIN as playwright

        BROWSER_REGISTRY.register(playwright)


__all__ = [
    "CONTROL_SCAN_CALL_LITERAL",
    "MAX_TEXT_CHARS",
    "detect_condition",
    "parse_scanned_controls",
    "register_browsers",
    "split_locator",
]
