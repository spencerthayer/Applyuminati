"""Playwright browser backend: the portable fallback.

Playwright is imported lazily inside functions so the package stays importable
without it installed. ``health()`` returns ``NOT_INSTALLED`` with an actionable
message when the import fails or browsers are not downloaded.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from applyuminati.browser.base import (
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
from applyuminati.core.ids import new_ulid
from applyuminati.core.logging import get_logger
from applyuminati.core.registry import HealthReport, HealthState, PluginMaturity
from applyuminati.core.settings import Settings
from applyuminati.plugins.browsers.shared import (
    MAX_TEXT_CHARS,
    detect_condition,
    questions_from_elements,
)

log = get_logger(__name__)

__all__ = [
    "PLUGIN",
    "PlaywrightBackend",
    "PlaywrightControl",
    "PlaywrightSession",
    "build_playwright_locator",
    "checkbox_checked_from_answer",
    "elements_from_metadata",
    "radio_answer_matches",
]

#: What this backend can do regardless of configuration.
#:
#: Notably absent: HUMAN_HANDOFF, PERSISTENT_SESSION and
#: AUTHENTICATED_USER_PROFILE. A browser we launched ourselves is not the user's
#: signed-in profile, its context dies with the process, and there is no way to
#: hand a container's browser to a person or to learn when they are done. Those
#: absences are the point: they are what stops an authenticated application from
#: being routed here.
_CAPABILITIES = frozenset(
    {
        BrowserCapability.NAVIGATE,
        BrowserCapability.SEMANTIC_SNAPSHOT,
        BrowserCapability.SCREENSHOT,
        BrowserCapability.FILE_UPLOAD,
        BrowserCapability.HEADLESS,
        BrowserCapability.JAVASCRIPT_EVAL,
        BrowserCapability.MULTI_TAB,
    }
)

#: Earned only when ``browser.playwright_storage_state`` is set, because that is
#: the only configuration in which cookies are loaded and saved.
_STORAGE_CAPABILITIES = frozenset({BrowserCapability.PERSISTENT_LOGIN})


def _metadata(settings: Settings | None = None) -> BrowserMetadata:
    capabilities = _CAPABILITIES
    if settings is not None and settings.browser.playwright_storage_state is not None:
        capabilities = capabilities | _STORAGE_CAPABILITIES
    return BrowserMetadata(
        slug="playwright",
        name="Playwright",
        capabilities=capabilities,
        homepage="https://playwright.dev",
        notes=(
            "Portable default. Run `playwright install chromium` after pip install. "
            "Cannot hand its browser to a person, so applications needing a human "
            "at an authentication wall are not routed here."
        ),
    )


@dataclass(frozen=True, slots=True)
class PlaywrightControl:
    """Attributes Playwright uses to build a backend-owned locator string."""

    tag: str
    input_type: str | None = None
    element_id: str | None = None
    name: str | None = None
    value: str | None = None
    aria_label: str | None = None
    placeholder: str | None = None
    data_attr: str | None = None
    data_value: str | None = None
    aria_role: str | None = None
    index_in_type: int = 0


_CSS_ID_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_NATIVE_NTH_TAGS = frozenset({"input", "textarea", "select", "button", "a"})


def _css_attr(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{name}="{escaped}"]'


def _nth_group(control: PlaywrightControl) -> str:
    if control.aria_role:
        return f"role:{control.aria_role}"
    if control.input_type == "contenteditable":
        return "contenteditable"
    if control.tag == "input":
        if control.input_type:
            return f"input:{control.input_type}"
        return "input:not([type])"
    if control.tag == "a":
        return "a[href]"
    return control.tag


def _nth_selector(control: PlaywrightControl) -> str:
    """Selector whose nth index is counted only among matching visible nodes."""
    n = control.index_in_type
    if control.aria_role:
        escaped = control.aria_role.replace("'", "\\'")
        base = f"[role='{escaped}']"
    elif control.input_type == "contenteditable":
        base = ":is([contenteditable='true'], [contenteditable='']):not([role])"
    elif control.tag == "input" and control.input_type:
        escaped = control.input_type.replace("'", "\\'")
        base = f"input[type='{escaped}']:not([role])"
    elif control.tag == "input":
        base = "input:not([type]):not([role])"
    elif control.tag == "a":
        base = "a[href]:not([role])"
    elif control.tag in _NATIVE_NTH_TAGS:
        base = f"{control.tag}:not([role])"
    else:
        base = f"{control.tag}:not([role])"
    return f"{base} >> visible=true >> nth={n}"


def _id_selector(element_id: str) -> str:
    if _CSS_ID_RE.match(element_id):
        return f"#{element_id}"
    return _css_attr("id", element_id)


def _locator_candidates(control: PlaywrightControl) -> list[str]:
    """Attribute selectors that may address this control. nth is separate."""
    candidates: list[str] = []
    if control.element_id:
        candidates.append(_id_selector(control.element_id))
    if control.input_type == "radio" and control.name and control.value is not None:
        candidates.append(_css_attr("name", control.name) + _css_attr("value", control.value))
    elif control.name:
        candidates.append(_css_attr("name", control.name))
    if control.aria_label:
        candidates.append(_css_attr("aria-label", control.aria_label))
    if control.placeholder:
        candidates.append(_css_attr("placeholder", control.placeholder))
    if control.data_attr and control.data_value:
        candidates.append(_css_attr(control.data_attr, control.data_value))
    return candidates


def _count_matching(
    peers: Sequence[PlaywrightControl],
    predicate: Callable[[PlaywrightControl], bool],
) -> int:
    return sum(1 for peer in peers if predicate(peer))


def _flag_or_peer_unique(row: dict[str, Any], key: str, peer_count: int) -> bool:
    if key in row:
        return bool(row[key])
    return peer_count == 1


def _unique_selectors(
    control: PlaywrightControl,
    peers: Sequence[PlaywrightControl],
    row: dict[str, Any],
) -> set[str]:
    """Selectors that match exactly one node in the live DOM or scanned peers."""
    unique = {_nth_selector(control)}
    if control.element_id and _flag_or_peer_unique(
        row,
        "uniqueId",
        _count_matching(peers, lambda p: p.element_id == control.element_id),
    ):
        unique.add(_id_selector(control.element_id))
    if control.input_type == "radio" and control.name and control.value is not None:
        if _flag_or_peer_unique(
            row,
            "uniqueNameValue",
            _count_matching(peers, lambda p: p.name == control.name and p.value == control.value),
        ):
            unique.add(_css_attr("name", control.name) + _css_attr("value", control.value))
    elif control.name and _flag_or_peer_unique(
        row, "uniqueName", _count_matching(peers, lambda p: p.name == control.name)
    ):
        unique.add(_css_attr("name", control.name))
    if control.aria_label and _flag_or_peer_unique(
        row, "uniqueAriaLabel", _count_matching(peers, lambda p: p.aria_label == control.aria_label)
    ):
        unique.add(_css_attr("aria-label", control.aria_label))
    if control.placeholder and _flag_or_peer_unique(
        row,
        "uniquePlaceholder",
        _count_matching(peers, lambda p: p.placeholder == control.placeholder),
    ):
        unique.add(_css_attr("placeholder", control.placeholder))
    if (
        control.data_attr
        and control.data_value
        and _flag_or_peer_unique(
            row,
            "uniqueData",
            _count_matching(
                peers,
                lambda p: p.data_attr == control.data_attr and p.data_value == control.data_value,
            ),
        )
    ):
        unique.add(_css_attr(control.data_attr, control.data_value))
    return unique


def build_playwright_locator(
    control: PlaywrightControl,
    *,
    used: set[str],
    unique_in_dom: set[str] | None = None,
) -> str:
    """Return a locator that matches one control, not merely a new string."""
    for candidate in _locator_candidates(control):
        if candidate in used:
            continue
        if unique_in_dom is not None and candidate not in unique_in_dom:
            continue
        return candidate
    return _nth_selector(control)


_CHECKBOX_TRUE = frozenset({"yes", "true", "y", "1", "on"})
_CHECKBOX_FALSE = frozenset({"no", "false", "n", "0", "off"})


def checkbox_checked_from_answer(answer: str) -> bool | None:
    """True/False for a yes/no checkbox answer, or None if the value is unknown."""
    folded = answer.strip().casefold()
    if folded in _CHECKBOX_TRUE:
        return True
    if folded in _CHECKBOX_FALSE:
        return False
    return None


def radio_answer_matches(
    *, option_value: str | None, option_label: str | None, answer: str
) -> bool:
    """True when ``answer`` is this radio's value or accessible name."""
    needle = answer.strip().casefold()
    if not needle:
        return False
    if option_value and option_value.strip().casefold() == needle:
        return True
    return bool(option_label and option_label.strip().casefold() == needle)


#: Playwright-only metadata scrape. Locator strings are built in Python.
#: Role selectors here must stay in sync with every role `_nth_group` buckets.
_CONTROL_METADATA_JS = """() => {
  const selector = [
    'input:not([type="hidden"])',
    'textarea',
    'select',
    'button',
    'a[href]',
    '[contenteditable="true"]',
    '[contenteditable=""]',
    '[role="combobox"]',
    '[role="textbox"]',
    '[role="searchbox"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="button"]',
    '[role="link"]'
  ].join(', ');
  function accessibleName(el) {
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return (lab.innerText || '').trim();
    }
    const wrap = el.closest('label');
    if (wrap) {
      const clone = wrap.cloneNode(true);
      clone.querySelectorAll('input, textarea, select, button').forEach((n) => n.remove());
      return (clone.innerText || '').trim();
    }
    if (el.tagName === 'BUTTON' || el.tagName === 'A' || el.getAttribute('role') === 'button') {
      return (el.innerText || el.textContent || '').trim() || null;
    }
    return null;
  }
  function optionsFor(el) {
    if (el.tagName !== 'SELECT') return [];
    return Array.from(el.options).map((o) => (o.text || '').trim()).filter(Boolean);
  }
  function cssAttr(name, value) {
    const escaped = String(value).replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
    return '[' + name + '="' + escaped + '"]';
  }
  function uniqueCss(sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  }
  function idSelector(id) {
    return /^[A-Za-z_][\\w-]*$/.test(id) ? ('#' + id) : cssAttr('id', id);
  }
  const seen = new Set();
  const rows = [];
  document.querySelectorAll(selector).forEach((el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (style.visibility === 'hidden' || style.visibility === 'collapse') return;
    if (rect.width === 0 || rect.height === 0) return;
    if (seen.has(el)) return;
    seen.add(el);
    const dataTestid = el.getAttribute('data-testid');
    const dataQa = el.getAttribute('data-qa');
    const dataAttr = dataTestid ? 'data-testid' : (dataQa ? 'data-qa' : null);
    const dataValue = dataTestid || dataQa;
    const id = el.id || null;
    const name = el.getAttribute('name');
    const value = el.getAttribute('value');
    const type = el.getAttribute('type');
    const ariaLabel = (el.getAttribute('aria-label') || '').trim() || null;
    const placeholder = el.getAttribute('placeholder');
    const idSel = id ? idSelector(id) : null;
    rows.push({
      tag: el.tagName.toLowerCase(),
      type: type,
      id: id,
      name: name,
      value: value,
      placeholder: placeholder,
      accessibleName: accessibleName(el),
      ariaLabel: ariaLabel,
      ariaRole: el.getAttribute('role'),
      dataAttr: dataAttr,
      dataValue: dataValue,
      uniqueId: idSel ? uniqueCss(idSel) : false,
      uniqueName: name ? uniqueCss(cssAttr('name', name)) : false,
      uniqueNameValue: (String(type).toLowerCase() === 'radio' && name && value != null)
        ? uniqueCss(cssAttr('name', name) + cssAttr('value', value)) : false,
      uniqueAriaLabel: ariaLabel ? uniqueCss(cssAttr('aria-label', ariaLabel)) : false,
      uniquePlaceholder: placeholder ? uniqueCss(cssAttr('placeholder', placeholder)) : false,
      uniqueData: (dataAttr && dataValue) ? uniqueCss(cssAttr(dataAttr, dataValue)) : false,
      required: el.hasAttribute('required'),
      disabled: el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true',
      contenteditable: el.isContentEditable && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA',
      options: optionsFor(el)
    });
  });
  return rows;
}"""


_BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})


def _classify_toggle(
    input_type: str | None, aria_role: str | None
) -> tuple[ElementRole, str | None] | None:
    if input_type == "checkbox":
        return ElementRole.CHECKBOX, input_type
    if aria_role == "checkbox":
        return ElementRole.CHECKBOX, "aria-checkbox"
    if input_type == "radio":
        return ElementRole.RADIO, input_type
    if aria_role == "radio":
        return ElementRole.RADIO, "aria-radio"
    return None


def _classify_remaining(
    tag: str, input_type: str | None, aria_role: str | None
) -> tuple[ElementRole, str | None]:
    toggle = _classify_toggle(input_type, aria_role)
    if toggle is not None:
        return toggle
    role = ElementRole.TEXTBOX
    if aria_role == "combobox":
        input_type = input_type or "combobox"
    elif aria_role == "searchbox":
        input_type = "search"
    elif aria_role == "button":
        role = ElementRole.BUTTON
    else:
        input_type = input_type or ("text" if tag == "input" else None)
    return role, input_type


def _classify_control(row: dict[str, Any]) -> tuple[ElementRole, str | None]:
    tag = str(row.get("tag") or "")
    raw_type = row.get("type")
    input_type = str(raw_type).lower() if raw_type else None
    aria_role = (str(row.get("ariaRole") or "")).lower() or None
    role = ElementRole.TEXTBOX
    if row.get("contenteditable"):
        return ElementRole.TEXTBOX, "contenteditable"
    if tag == "select":
        role = ElementRole.SELECT
    elif aria_role == "combobox" and tag not in {"input", "textarea"}:
        role = ElementRole.SELECT
        input_type = "combobox"
    elif tag == "textarea":
        role = ElementRole.TEXTAREA
    elif tag == "button" or input_type in _BUTTON_INPUT_TYPES:
        role = ElementRole.BUTTON
    elif tag == "a" or aria_role == "link":
        role = ElementRole.LINK
    elif input_type == "file":
        role = ElementRole.FILE_INPUT
        input_type = "file"
    else:
        return _classify_remaining(tag, input_type, aria_role)
    return role, input_type


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _data_attr_from_row(row: dict[str, Any]) -> tuple[str | None, str | None]:
    data_attr = _optional_str(row.get("dataAttr"))
    data_value = _optional_str(row.get("dataValue"))
    if data_value is None:
        data_value = _optional_str(row.get("dataTestid"))
        if data_value is not None and data_attr is None:
            data_attr = "data-testid"
    return data_attr, data_value


def _staged_controls(
    rows: Sequence[Any],
) -> list[tuple[dict[str, Any], PlaywrightControl, ElementRole]]:
    group_counts: dict[str, int] = {}
    staged: list[tuple[dict[str, Any], PlaywrightControl, ElementRole]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        role, classified_type = _classify_control(row)
        tag = str(row.get("tag") or "")
        aria_role = _optional_str(row.get("ariaRole"))
        if aria_role:
            aria_role = aria_role.lower()
        data_attr, data_value = _data_attr_from_row(row)
        locator_type = classified_type
        if tag == "input" and classified_type != "contenteditable":
            raw_type = _optional_str(row.get("type"))
            locator_type = raw_type.lower() if raw_type else None
        staged_control = PlaywrightControl(
            tag=tag,
            input_type=locator_type,
            element_id=_optional_str(row.get("id")),
            name=_optional_str(row.get("name")),
            value=None if row.get("value") is None else str(row.get("value")),
            aria_label=_optional_str(row.get("ariaLabel")),
            placeholder=_optional_str(row.get("placeholder")),
            data_attr=data_attr,
            data_value=data_value,
            aria_role=aria_role,
        )
        group = _nth_group(staged_control)
        index = group_counts.get(group, 0)
        group_counts[group] = index + 1
        staged.append((row, replace(staged_control, index_in_type=index), role))
    return staged


def _page_element(
    row: dict[str, Any], control: PlaywrightControl, locator: str, role: ElementRole
) -> PageElement:
    accessible = _optional_str(row.get("accessibleName")) or control.aria_label
    options_raw = row.get("options") or []
    options = [str(item) for item in options_raw if item] if isinstance(options_raw, list) else []
    _, classified_type = _classify_control(row)
    return PageElement(
        locator=locator,
        role=role,
        label=accessible,
        name=control.name,
        value=control.value,
        placeholder=control.placeholder,
        required=bool(row.get("required")),
        disabled=bool(row.get("disabled")),
        options=options,
        input_type=classified_type,
    )


def elements_from_metadata(rows: Sequence[Any]) -> list[PageElement]:
    """Turn Playwright metadata rows into uniquely addressed page elements."""
    staged = _staged_controls(rows)
    peers = [control for _, control, _role in staged]
    used: set[str] = set()
    elements: list[PageElement] = []
    for row, control, role in staged:
        unique = _unique_selectors(control, peers, row)
        locator = build_playwright_locator(control, used=used, unique_in_dom=unique)
        used.add(locator)
        elements.append(_page_element(row, control, locator, role))
    return elements


class PlaywrightSession(BrowserSession):
    def __init__(self, page: Any, browser: Any, session_id: str, settings: Settings) -> None:
        self._page = page
        self._browser = browser
        self._session_id = session_id
        self._settings = settings
        self._owner = ControlOwner.AGENT

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    @property
    def task_space_id(self) -> str | None:
        """None: a Playwright context does not outlive this process."""
        return None

    async def navigate(self, url: str, *, wait_for_load: bool = True) -> PageObservation:
        await self._page.goto(url, wait_until="domcontentloaded" if wait_for_load else "commit")
        return await self.observe()

    async def observe(self, *, include_text: bool = True) -> PageObservation:
        url = self._page.url
        title = await self._page.title()
        text = await self._page.inner_text("body") if include_text else None
        if text is not None:
            text = text[:MAX_TEXT_CHARS]
        elements = await self._extract_controls()
        has_password_field = any(
            element.input_type == "password" or (element.name or "").lower() == "password"
            for element in elements
        )
        validation_errors = [element.error_text for element in elements if element.error_text]
        return PageObservation(
            url=url,
            title=title,
            text=text,
            elements=elements,
            questions=questions_from_elements(elements),
            condition=detect_condition(
                url,
                text,
                has_password_field=has_password_field,
                validation_errors=validation_errors,
            ),
            validation_errors=validation_errors,
        )

    async def find_controls(self, *, role: ElementRole | None = None) -> list[PageElement]:
        elements = await self._extract_controls()
        if role is not None:
            elements = [el for el in elements if el.role is role]
        return elements

    async def _fill_radio(self, locator: str, value: str, start: float) -> ActionResult:
        first = self._page.locator(locator).first
        name = await first.get_attribute("name")
        group = (
            self._page.locator(
                f'input[type="radio"]{_css_attr("name", name)}, '
                f'[role="radio"]{_css_attr("name", name)}'
            )
            if name
            else self._page.locator(locator)
        )
        count = await group.count()
        for index in range(count):
            radio = group.nth(index)
            option_value = await radio.get_attribute("value")
            option_label = await radio.evaluate(
                """el => {
                  const aria = (el.getAttribute('aria-label') || '').trim();
                  if (aria) return aria;
                  if (el.id) {
                    const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                    if (lab) return (lab.innerText || '').trim();
                  }
                  const wrap = el.closest('label');
                  return wrap ? (wrap.innerText || '').trim() : '';
                }"""
            )
            if not radio_answer_matches(
                option_value=option_value,
                option_label=option_label or None,
                answer=value,
            ):
                continue
            await radio.check()
            return ActionResult(
                ok=True, action="fill", duration_ms=(time.perf_counter() - start) * 1000
            )
        return ActionResult(
            ok=False,
            action="fill",
            detail=f"no radio matching {value!r}",
            duration_ms=(time.perf_counter() - start) * 1000,
        )

    async def fill_field(self, locator: str, value: str) -> ActionResult:
        start = time.perf_counter()
        try:
            target = self._page.locator(locator).first
            input_type = (await target.get_attribute("type") or "").lower()
            aria_role = (await target.get_attribute("role") or "").lower()
            tag = await target.evaluate("el => el.tagName")
            if input_type == "radio" or aria_role == "radio":
                return await self._fill_radio(locator, value, start)
            if input_type == "checkbox" or aria_role == "checkbox":
                checked = checkbox_checked_from_answer(value)
                if checked is None:
                    return ActionResult(
                        ok=False,
                        action="fill",
                        detail=f"checkbox answer {value!r} is not a recognized yes/no value",
                        duration_ms=(time.perf_counter() - start) * 1000,
                    )
                await self._page.check(locator) if checked else await self._page.uncheck(locator)
                return ActionResult(
                    ok=True, action="fill", duration_ms=(time.perf_counter() - start) * 1000
                )
            if tag == "SELECT":
                await self._page.select_option(locator, value)
                return ActionResult(
                    ok=True, action="fill", duration_ms=(time.perf_counter() - start) * 1000
                )
            await self._page.fill(locator, value)
            return ActionResult(
                ok=True, action="fill", duration_ms=(time.perf_counter() - start) * 1000
            )
        except Exception as exc:
            return ActionResult(
                ok=False,
                action="fill",
                detail=str(exc),
                duration_ms=(time.perf_counter() - start) * 1000,
            )

    async def select_option(self, locator: str, option: str) -> ActionResult:
        start = time.perf_counter()
        try:
            await self._page.select_option(locator, option)
            return ActionResult(
                ok=True, action="select", duration_ms=(time.perf_counter() - start) * 1000
            )
        except Exception as exc:
            return ActionResult(ok=False, action="select", detail=str(exc))

    async def set_checked(self, locator: str, checked: bool) -> ActionResult:
        try:
            await self._page.check(locator) if checked else await self._page.uncheck(locator)
            return ActionResult(ok=True, action="check")
        except Exception as exc:
            return ActionResult(ok=False, action="check", detail=str(exc))

    async def upload_file(self, locator: str, path: Path) -> ActionResult:
        try:
            await self._page.set_input_files(locator, str(path))
            return ActionResult(ok=True, action="upload")
        except Exception as exc:
            return ActionResult(ok=False, action="upload", detail=str(exc))

    async def click(
        self,
        locator: str,
        *,
        label: str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        _ = idempotency_key
        try:
            await self._page.click(locator)
            return ActionResult(ok=True, action="click")
        except Exception as exc:
            return ActionResult(ok=False, action="click", detail=str(exc))

    async def wait_for_navigation(self, *, timeout_seconds: float | None = None) -> ActionResult:
        try:
            await self._page.wait_for_load_state(
                "domcontentloaded", timeout=int((timeout_seconds or 30) * 1000)
            )
            return ActionResult(ok=True, action="wait_navigation")
        except Exception as exc:
            return ActionResult(ok=False, action="wait_navigation", detail=str(exc))

    async def screenshot(self, *, relative_path: str) -> str:
        full_path = self._settings.artifacts_dir / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=str(full_path))
        return relative_path

    async def checkpoint(self) -> BrowserCheckpoint:
        return BrowserCheckpoint(
            session_id=self._session_id,
            url=self._page.url,
            backend_state={"type": "playwright"},
        )

    async def request_human_control(self, instruction: str) -> ActionResult:
        """Refuse, honestly.

        This used to set the owner and return success, which is worse than not
        having handoff at all: the caller believes a person is now signing in,
        and nobody is. There is no browser window to give away in a container,
        and even headed there is no ownership signal to tell us the human is
        finished, so we would have to guess with a timer and race them.

        Handoff comes from :attr:`BrowserCapability.HUMAN_HANDOFF`, which this
        backend does not advertise; selection therefore never routes an
        application needing it here. Reaching this method means a workflow asked
        anyway, and a clear refusal is what lets it escalate correctly.
        """
        return ActionResult(
            ok=False,
            action="request_human_control",
            detail=(
                "the playwright backend cannot hand its browser to a person; "
                "run a Browser Host with an interactive backend for this application"
            ),
            condition=PageCondition.UNKNOWN,
        )

    async def control_state(self) -> ControlOwner:
        """Always the agent: there is no other party in this browser."""
        return self._owner

    async def wait_for_control(self, *, timeout_seconds: float) -> ActionResult:
        """Nothing to wait for: this backend never gives control away."""
        if self._owner is ControlOwner.AGENT:
            return ActionResult(ok=True, action="wait_for_control", detail="already the owner")
        return ActionResult(
            ok=False,
            action="wait_for_control",
            detail="the playwright backend has no handoff to return from",
        )

    async def reclaim_control(self, *, confirmed_by_user: bool) -> ActionResult:
        """Nothing was ever handed over, so there is nothing to reclaim."""
        if not confirmed_by_user:
            return ActionResult(
                ok=False,
                action="reclaim_control",
                detail="reclaim requires an explicit user confirmation",
            )
        self._owner = ControlOwner.AGENT
        return ActionResult(ok=True, action="reclaim_control", detail="already the owner")

    async def close(self) -> None:
        try:
            await self._save_storage_state()
            await self._page.close()
            await self._browser.close()
        except Exception:
            log.debug("playwright.close_failed", exc_info=True)

    async def _save_storage_state(self) -> None:
        """Persist cookies so the PERSISTENT_LOGIN claim is actually true.

        The context was loading ``storage_state`` and never writing it, so a
        login survived exactly as long as the process. The capability is now
        advertised only when this path is configured, and it has to be earned.
        """
        destination = self._settings.browser.playwright_storage_state
        if destination is None:
            return
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            await self._page.context.storage_state(path=str(destination))
        except Exception:
            log.warning("playwright.storage_state_not_saved", path=str(destination))

    async def _extract_controls(self) -> list[PageElement]:
        try:
            rows = await self._page.evaluate(_CONTROL_METADATA_JS)
        except Exception:
            log.debug("playwright.control_scan_failed", exc_info=True)
            return []
        if not isinstance(rows, list):
            return []
        return elements_from_metadata(rows)


class PlaywrightBackend(BrowserBackend):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Any | None = None
        self._browser: Any | None = None

    @property
    def metadata(self) -> BrowserMetadata:
        return _metadata(self._settings)

    async def health(self) -> HealthReport:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return HealthReport(
                plugin="playwright",
                state=HealthState.NOT_INSTALLED,
                detail=(
                    "playwright is not installed; run `uv sync --all-extras` "
                    "then `playwright install chromium`"
                ),
            )
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                await browser.close()
            return HealthReport(
                plugin="playwright",
                state=HealthState.HEALTHY,
                detail="Chromium is installed and launchable",
            )
        except Exception as exc:
            return HealthReport(
                plugin="playwright",
                state=HealthState.NOT_INSTALLED,
                detail=f"Chromium not found; run `playwright install chromium`. Detail: {exc}",
            )

    async def open_session(
        self,
        *,
        session_id: str | None = None,
        resume: BrowserCheckpoint | None = None,
        task_space: str | None = None,
    ) -> BrowserSession:
        from playwright.async_api import async_playwright

        # Playwright has no workspace that outlives the context, so it cannot
        # honour a task-space name and does not claim PERSISTENT_SESSION.
        _ = task_space

        if self._playwright is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._settings.browser.headless
            )
        browser = self._browser
        if browser is None:
            msg = "playwright browser failed to start"
            raise RuntimeError(msg)
        context = await browser.new_context(
            storage_state=str(self._settings.browser.playwright_storage_state)
            if self._settings.browser.playwright_storage_state
            else None
        )
        page = await context.new_page()
        sid = session_id or new_ulid()
        return PlaywrightSession(page, browser, sid, self._settings)

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None


PLUGIN = browser_plugin(
    slug="playwright",
    name="Playwright",
    factory=PlaywrightBackend,
    description="Portable Playwright-based browser backend.",
    capabilities=_CAPABILITIES,
    priority=5,
    maturity=PluginMaturity.HEALTH_PROBE_WORKING,
)
