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
from collections.abc import Sequence
from typing import Any

from applyuminati.browser.base import ElementRole, PageCondition, PageElement
from applyuminati.core.models.questionnaire import ApplicationQuestion, QuestionKind

__all__ = [
    "CONTROL_SCAN_CALL_LITERAL",
    "MAX_TEXT_CHARS",
    "detect_condition",
    "parse_scanned_controls",
    "questions_from_elements",
    "split_locator",
]

#: JS expression that scans for interactive controls. Injected into ego lite
#: scripts. Playwright builds its own locators and does not evaluate this.
CONTROL_SCAN_CALL_LITERAL = """(function() {
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

#: Maximum text length we store from a page snapshot. Prevents a megabyte of
#: minified SPA HTML from consuming the whole observation.
MAX_TEXT_CHARS = 20_000

#: Patterns that indicate a bot interstitial, a login wall, or a human challenge.
_CONDITION_PATTERNS: tuple[tuple[re.Pattern[str], PageCondition], ...] = (
    (
        re.compile(r"captcha|are you a robot|please verify you are a human", re.I),
        PageCondition.HUMAN_CHALLENGE,
    ),
    (
        re.compile(r"access denied|blocked|bot detection|unusual traffic|ddos protection", re.I),
        PageCondition.AUTOMATION_BLOCKED,
    ),
    (
        re.compile(r"sign in|log in|please log in|login required|authenticate", re.I),
        PageCondition.LOGIN_REQUIRED,
    ),
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
    dialog_open: bool = False,
    has_password_field: bool = False,
    challenge_markers: int = 0,
    validation_errors: list[str] | None = None,
) -> PageCondition:
    """Detect the page condition from URL, text, and DOM-scan signals.

    Signal precedence: a blocking native dialog outranks everything else
    (nothing else can be observed reliably until it is dismissed); explicit
    validation errors from a submitted form outrank text heuristics; repeated
    challenge markers (e.g. multiple CAPTCHA iframes) outrank the weaker
    text-pattern match. Text patterns are checked last and are corroborated,
    not overridden, by ``has_password_field``.
    """
    if dialog_open:
        return PageCondition.DIALOG_OPEN
    if validation_errors:
        return PageCondition.VALIDATION_ERROR
    if challenge_markers > 0:
        return PageCondition.HUMAN_CHALLENGE
    haystack = f"{url} {text or ''}"[:5000]
    for pattern, condition in _CONDITION_PATTERNS:
        if pattern.search(haystack):
            return condition
    if has_password_field and re.search(r"\bpassword\b", haystack, re.I):
        return PageCondition.LOGIN_REQUIRED
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
                input_type=item.get("input_type") or item.get("inputType"),
            )
        )
    return elements


_APPLICANT_INPUT_ROLES: frozenset[ElementRole] = frozenset(
    {
        ElementRole.TEXTBOX,
        ElementRole.TEXTAREA,
        ElementRole.SELECT,
        ElementRole.CHECKBOX,
        ElementRole.RADIO,
    }
)
_SEARCH_RE = re.compile(r"\bsearch\b", re.I)


def _is_search_control(element: PageElement) -> bool:
    if element.input_type == "search":
        return True
    haystack = " ".join(part for part in (element.label, element.name, element.placeholder) if part)
    return bool(_SEARCH_RE.search(haystack))


_ROLE_QUESTION_KINDS: dict[ElementRole, QuestionKind] = {
    ElementRole.TEXTAREA: QuestionKind.LONG_TEXT,
    ElementRole.SELECT: QuestionKind.SINGLE_SELECT,
    ElementRole.CHECKBOX: QuestionKind.BOOLEAN,
    ElementRole.RADIO: QuestionKind.SINGLE_SELECT,
}
_INPUT_QUESTION_KINDS: dict[str, QuestionKind] = {
    "number": QuestionKind.NUMBER,
    "date": QuestionKind.DATE,
    "url": QuestionKind.URL,
}


def _question_kind(element: PageElement) -> QuestionKind:
    role_kind = _ROLE_QUESTION_KINDS.get(element.role)
    if role_kind is not None:
        return role_kind
    return _INPUT_QUESTION_KINDS.get(element.input_type or "", QuestionKind.SHORT_TEXT)


_BOOLEAN_OPTION_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"yes", "no"}),
        frozenset({"true", "false"}),
        frozenset({"y", "n"}),
    }
)


def _radio_option_text(radio: PageElement, group: Sequence[PageElement]) -> str:
    label = (radio.label or "").strip()
    labels = [(item.label or "").strip() for item in group]
    if label and labels.count(label) == 1:
        return label
    if radio.value:
        return str(radio.value)
    return label


def _radio_question_text(group: Sequence[PageElement]) -> str:
    labels = {(item.label or "").strip() for item in group if (item.label or "").strip()}
    if len(labels) == 1:
        return next(iter(labels))
    name = (group[0].name or "").strip()
    if name:
        return name.replace("_", " ")
    return next(iter(labels), "")


def _radio_question_kind(options: Sequence[str]) -> QuestionKind:
    folded = frozenset(option.strip().lower() for option in options)
    if folded in _BOOLEAN_OPTION_PAIRS:
        return QuestionKind.BOOLEAN
    return QuestionKind.SINGLE_SELECT


def questions_from_elements(elements: Sequence[PageElement]) -> list[ApplicationQuestion]:
    """Map labelled applicant-input controls to questions.

    Buttons, links, uploads, navigation, search boxes, and generic
    contenteditable regions stay as :class:`PageElement` only. A question is
    emitted only when a label or accessibility name, an applicant-input role,
    and a locator all exist. Radios that share a ``name`` become one question.
    """
    questions: list[ApplicationQuestion] = []
    seen_radio_names: set[str] = set()
    for element in elements:
        if not element.locator:
            continue
        if element.role not in _APPLICANT_INPUT_ROLES:
            continue
        label = (element.label or "").strip()
        if not label:
            continue
        if element.input_type == "contenteditable":
            continue
        if _is_search_control(element):
            continue
        if element.role is ElementRole.RADIO and element.name:
            if element.name in seen_radio_names:
                continue
            seen_radio_names.add(element.name)
            group = [
                item
                for item in elements
                if item.role is ElementRole.RADIO and item.name == element.name
            ]
            options: list[str] = []
            for radio in group:
                option_text = _radio_option_text(radio, group)
                if option_text:
                    options.append(option_text)
            text = _radio_question_text(group)
            if not text:
                continue
            questions.append(
                ApplicationQuestion(
                    text=text,
                    kind=_radio_question_kind(options),
                    required=any(item.required for item in group),
                    options=options,
                    field_locator=element.locator,
                )
            )
            continue
        questions.append(
            ApplicationQuestion(
                text=label,
                kind=_question_kind(element),
                required=element.required,
                options=list(element.options),
                field_locator=element.locator,
            )
        )
    return questions


def split_locator(locator: str) -> tuple[str, str]:
    """Split a locator into (engine, target).

    Supports ``css=...``, ``role=...``, ``xpath=...``, ``ref=...``, and bare
    CSS selectors (assumed CSS when no prefix is present).
    """
    for prefix in ("css=", "role=", "xpath=", "ref=", "aria="):
        if locator.startswith(prefix):
            return prefix[:-1], locator[len(prefix) :]
    return "css", locator
