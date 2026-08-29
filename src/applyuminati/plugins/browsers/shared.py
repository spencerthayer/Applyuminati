"""Shared helpers for browser backend plugins.

Page-condition detection, control parsing, and locator splitting are common to
every browser backend — ego lite returns a snapshot, Playwright returns an
a11y tree, but both need the same downstream logic. Keeping them here means a
new backend reuses them rather than reimplementing (and diverging on) the
detection rules.
"""

from __future__ import annotations

import json
import re
from typing import Any

from applyuminati.browser.base import ElementRole, PageCondition, PageElement

__all__ = [
    "CONTROL_SCAN_CALL_LITERAL",
    "MAX_TEXT_CHARS",
    "detect_condition",
    "parse_scanned_controls",
    "split_locator",
]

#: JS expression that scans for interactive controls. Injected into ego lite
#: scripts and evaluated by the Playwright backend's JS eval.
CONTROL_SCAN_CALL_LITERAL = (
    """(function() {
  const controls = [];
  const sel = 'input, textarea, select, button, a[href], [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="radio"]';
  document.querySelectorAll(sel).forEach(function(el) {
    if (el.offsetParent === null && el.tagName !== 'INPUT') return;
    const role = (el.tagName === 'INPUT' && el.type === 'file') ? 'file_input'
      : el.tagName === 'TEXTAREA' ? 'textarea'
      : el.tagName === 'SELECT' ? 'select'
      : el.tagName === 'INPUT' ? (el.type === 'checkbox' ? 'checkbox' : el.type === 'radio' ? 'radio' : 'textbox')
      : el.tagName === 'BUTTON' ? 'button'
      : el.tagName === 'A' ? 'link'
      : el.getAttribute('role') || 'other';
    controls.push({
      role: role,
      label: el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || (el.textContent || '').trim().slice(0, 80) || null,
      name: el.getAttribute('name') || null,
      value: el.getAttribute('value') || null,
      placeholder: el.getAttribute('placeholder') || null,
      required: el.hasAttribute('required'),
      disabled: el.hasAttribute('disabled'),
      options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean) : [],
      locator: el.id ? '#' + el.id : el.name ? `[name="${el.name}"]` : el.className ? '.' + el.className.split(' ')[0] : null,
      errorText: null,
    });
  });
  return controls;
})()"""
)

#: Maximum text length we store from a page snapshot. Prevents a megabyte of
#: minified SPA HTML from consuming the whole observation.
MAX_TEXT_CHARS = 20_000

#: Patterns that indicate a bot interstitial, a login wall, or a human challenge.
_CONDITION_PATTERNS: tuple[tuple[re.Pattern[str], PageCondition], ...] = (
    (re.compile(r"captcha|are you a robot|please verify you are a human", re.I), PageCondition.HUMAN_CHALLENGE),
    (re.compile(r"access denied|blocked|bot detection|unusual traffic|ddos protection", re.I), PageCondition.AUTOMATION_BLOCKED),
    (re.compile(r"sign in|log in|please log in|login required|authenticate", re.I), PageCondition.LOGIN_REQUIRED),
    (re.compile(r"rate limit|too many requests|slow down", re.I), PageCondition.RATE_LIMITED),
    (re.compile(r"404|not found|page does not exist", re.I), PageCondition.NOT_FOUND),
)

_ROLE_MAP: dict[str, ElementRole] = {
    "textbox": ElementRole.TEXTBOX,
    "textarea": ElementRole.TEXTAREA,
    "select": ElementRole.SELECT,
    "checkbox": ElementRole.CHECKBOX,
    "radio": ElementRole.RADIO,
    "button": ElementRole.BUTTON,
    "link": ElementRole.LINK,
    "file_input": ElementRole.FILE_INPUT,
    "other": ElementRole.OTHER,
}


def detect_condition(
    url: str,
    text: str | None,
    *,
    extra_signals: dict[str, Any] | None = None,
) -> PageCondition:
    """Detect the page condition from URL and text content."""
    if extra_signals and extra_signals.get("dialog"):
        return PageCondition.DIALOG_OPEN
    haystack = f"{url} {text or ''}"[:5000]
    for pattern, condition in _CONDITION_PATTERNS:
        if pattern.search(haystack):
            return condition
    return PageCondition.OK


def parse_scanned_controls(scan: Any) -> list[PageElement]:
    """Parse a JS control scan result into :class:`PageElement` objects."""
    if scan is None:
        return []
    if isinstance(scan, dict) and "scan_error" in scan:
        return []
    if isinstance(scan, str):
        try:
            scan = json.loads(scan)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(scan, list):
        return []
    elements: list[PageElement] = []
    for item in scan:
        if not isinstance(item, dict):
            continue
        locator = item.get("locator")
        if not locator:
            continue
        role_str = item.get("role", "other")
        elements.append(
            PageElement(
                locator=locator,
                role=_ROLE_MAP.get(role_str, ElementRole.OTHER),
                label=item.get("label"),
                name=item.get("name"),
                value=item.get("value"),
                placeholder=item.get("placeholder"),
                required=bool(item.get("required")),
                disabled=bool(item.get("disabled")),
                options=item.get("options") or [],
                error_text=item.get("errorText"),
            )
        )
    return elements


def split_locator(locator: str) -> tuple[str, str]:
    """Split a locator into (engine, target).

    Supports ``css=...``, ``role=...``, ``xpath=...``, ``ref=...``, and bare
    CSS selectors (assumed CSS when no prefix is present).
    """
    for prefix in ("css=", "role=", "xpath=", "ref=", "aria="):
        if locator.startswith(prefix):
            return prefix[:-1], locator[len(prefix) :]
    return "css", locator
