"""Playwright browser backend: the portable fallback.

Playwright is imported lazily inside functions so the package stays importable
without it installed. ``health()`` returns ``NOT_INSTALLED`` with an actionable
message when the import fails or browsers are not downloaded.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    BrowserDownload,
    BrowserMetadata,
    BrowserSession,
    BrowserTab,
    ControlOwner,
    ElementRole,
    PageCondition,
    PageElement,
    PageObservation,
    browser_plugin,
    session_closed_error,
)
from applyuminati.browser.downloads import ensure_download_directory, resolve_download_path
from applyuminati.core.errors import (
    ApplyuminatiError,
    BackendUnavailableError,
    DuplicateActionError,
    FailureCategory,
)
from applyuminati.core.ids import new_ulid
from applyuminati.core.logging import get_logger
from applyuminati.core.registry import HealthReport, HealthState, PluginMaturity
from applyuminati.core.settings import Settings
from applyuminati.plugins.browsers.playwright_persistence import StorageStateStore
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
#:
#: MULTI_TAB and DOWNLOADS are claimed because :class:`PlaywrightSession`
#: implements them, not because Playwright is capable of them in principle. What
#: a *Browser Host* advertises is narrower still: see
#: :data:`applyuminati.host.dispatcher.HOST_UNDISPATCHABLE_CAPABILITIES`.
_CAPABILITIES = frozenset(
    {
        BrowserCapability.NAVIGATE,
        BrowserCapability.SEMANTIC_SNAPSHOT,
        BrowserCapability.SCREENSHOT,
        BrowserCapability.FILE_UPLOAD,
        BrowserCapability.HEADLESS,
        BrowserCapability.JAVASCRIPT_EVAL,
        BrowserCapability.MULTI_TAB,
        BrowserCapability.DOWNLOADS,
    }
)

#: Earned only when ``browser.playwright_storage_state`` is set.
#:
#: ``PERSISTENT_LOGIN`` means the backend is configured to preserve and restore
#: authentication state between contexts and runs. It does not mean valid
#: authenticated state already exists. A configured path with no file yet is
#: still this capability: the first run establishes the jar. Runtime metadata
#: does not inspect the persistence store.
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


def _launch_options(settings: Settings) -> dict[str, Any]:
    """Playwright ``chromium.launch`` kwargs derived from settings.

    Keys whose value would be ``None`` are omitted so callers do not depend on
    Playwright treating explicit nulls the same as absent arguments. Proxy
    credentials are unwrapped here and nowhere else.
    """
    browser = settings.browser
    options: dict[str, object] = {"headless": browser.headless}
    if browser.playwright_channel is not None:
        options["channel"] = browser.playwright_channel
    if browser.playwright_executable_path is not None:
        path = browser.playwright_executable_path
        if not path.is_file():
            raise BackendUnavailableError(
                f"playwright executable {path.name!r} is missing or not a file",
                code="browser.playwright_binary_missing",
                details={"filename": path.name},
            )
        options["executable_path"] = str(path)
    if browser.playwright_proxy is not None:
        proxy: dict[str, object] = {"server": browser.playwright_proxy.server}
        if browser.playwright_proxy.username is not None:
            proxy["username"] = browser.playwright_proxy.username.get_secret_value()
        if browser.playwright_proxy.password is not None:
            proxy["password"] = browser.playwright_proxy.password.get_secret_value()
        if browser.playwright_proxy.bypass is not None:
            proxy["bypass"] = browser.playwright_proxy.bypass
        options["proxy"] = proxy
    return options


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
#: Must match the ``[role=...]`` entries in ``_CONTROL_METADATA_JS``.
_SCANNED_ROLES = frozenset(
    {"combobox", "textbox", "searchbox", "checkbox", "radio", "button", "link"}
)


def _css_attr(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{name}="{escaped}"]'


def _nth_group(control: PlaywrightControl) -> str:
    if control.aria_role and control.aria_role in _SCANNED_ROLES:
        group = f"role:{control.aria_role}"
    elif control.aria_role:
        group = f"{control.tag}[role={control.aria_role}]"
    elif control.input_type == "contenteditable":
        group = "contenteditable"
    elif control.tag == "input":
        group = f"input:{control.input_type}" if control.input_type else "input:not([type])"
    elif control.tag == "a":
        group = "a[href]"
    else:
        group = control.tag
    return group


def _nth_selector(control: PlaywrightControl) -> str:
    """Selector whose nth index is counted only among matching visible nodes."""
    n = control.index_in_type
    if control.aria_role and control.aria_role in _SCANNED_ROLES:
        escaped = control.aria_role.replace("'", "\\'")
        base = f"[role='{escaped}']"
    elif control.aria_role:
        escaped = control.aria_role.replace("'", "\\'")
        base = f"{control.tag}[role='{escaped}']"
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
      options: optionsFor(el),
      formScope: el.form ? ('form:' + Array.from(document.forms).indexOf(el.form)) : 'document'
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
    if aria_role == "searchbox":
        input_type = "search"
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
        form_scope=_optional_str(row.get("formScope")),
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


def _unsupported_aria_fill(
    tag: str, input_type: str, aria_role: str, start: float
) -> ActionResult | None:
    if tag == "INPUT":
        return None
    if input_type == "radio" or aria_role == "radio":
        widget = "radio"
    elif input_type == "checkbox" or aria_role == "checkbox":
        widget = "checkbox"
    else:
        return None
    return ActionResult(
        ok=False,
        action="fill",
        detail=f"custom ARIA {widget} is not fillable",
        duration_ms=(time.perf_counter() - start) * 1000,
    )


#: What every operation reports once the context is gone. Mirrors ego lite,
#: where a call on a closed session is an answer rather than an exception.
_CLOSED_DETAIL = "browser session is closed"


class PlaywrightSession(BrowserSession):
    """One Playwright ``BrowserContext`` and every page inside it.

    The session owns the context and nothing above it. The browser process
    belongs to :class:`PlaywrightBackend` and is shared with every other live
    session, so :meth:`close` closes this context and leaves the browser
    running. The previous arrangement handed each session the browser and let it
    close that on the way out, which meant the first attempt to finish killed
    the pages of every attempt still running beside it.

    Within the context the session may hold several tabs. Exactly one is active
    at a time, and every page-scoped operation resolves :attr:`_page` fresh
    rather than holding the page it was constructed with.
    """

    def __init__(
        self,
        context: Any,
        *,
        session_id: str,
        settings: Settings,
        on_close: Callable[[PlaywrightSession], None] | None = None,
        store: StorageStateStore | None = None,
        loaded_storage_generation: int = 0,
    ) -> None:
        self._context = context
        self._session_id = session_id
        self._settings = settings
        self._owner = ControlOwner.AGENT
        self._on_close = on_close
        self._store = store
        self._loaded_storage_generation = loaded_storage_generation
        self._closed = False
        self._active_page: Any | None = None
        #: Page object to the id callers hold. Ids are handed out once and never
        #: reused, so a caller cannot address a new tab with an old id.
        self._tab_ids: dict[Any, str] = {}
        self._tab_counter = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def task_space_id(self) -> str | None:
        """None: a Playwright context does not outlive this process."""
        return None

    # -- active page ------------------------------------------------------

    def _open_pages(self) -> list[Any]:
        if self._context is None or self._closed:
            return []
        return [page for page in self._context.pages if not page.is_closed()]

    def _track_pages(self) -> list[Any]:
        """Give ids to pages we have not seen, and forget ones that closed.

        Pages arrive here without having been opened through us. A link
        targeting ``_blank`` puts one in the context directly, which is how ATS
        portals show a privacy notice or an OAuth window, and a caller who could
        not see it would have no way to read or dismiss it.
        """
        pages = self._open_pages()
        for page in pages:
            if page not in self._tab_ids:
                self._tab_counter += 1
                self._tab_ids[page] = f"tab-{self._tab_counter}"
        live = {id(page) for page in pages}
        for gone in [page for page in self._tab_ids if id(page) not in live]:
            del self._tab_ids[gone]
        return pages

    def _resolve_active_page(self) -> Any | None:
        """The page every page-scoped operation acts on right now."""
        page = self._active_page
        if page is not None and not page.is_closed():
            return page
        pages = self._open_pages()
        self._active_page = pages[0] if pages else None
        return self._active_page

    @property
    def _page(self) -> Any:
        """The active page, resolved per call rather than captured at birth.

        Reading this on every operation is what makes :meth:`activate_tab`
        actually redirect them, and it is why closing a tab cannot leave the
        session driving a page that no longer exists.

        Raises on a closed session. The action methods below already turn a
        raised exception into ``ok=False``, so that is the closed-session answer
        they report.
        """
        page = self._resolve_active_page()
        if page is None:
            raise session_closed_error(self._session_id)
        return page

    @staticmethod
    async def _safe_title(page: Any) -> str | None:
        try:
            return (await page.title()) or None
        except Exception:
            # A tab mid-navigation still belongs in the list; dropping it would
            # hide the popup the caller is looking for.
            return None

    def _closed_result(self, action: str) -> ActionResult:
        return ActionResult(
            ok=False, action=action, detail=_CLOSED_DETAIL, condition=PageCondition.UNKNOWN
        )

    def _closed_observation(self, url: str) -> PageObservation:
        return PageObservation(url=url, condition=PageCondition.UNKNOWN, text=_CLOSED_DETAIL)

    # -- navigation and observation ---------------------------------------

    async def navigate(self, url: str, *, wait_for_load: bool = True) -> PageObservation:
        if self._closed:
            return self._closed_observation(url)
        await self._page.goto(url, wait_until="domcontentloaded" if wait_for_load else "commit")
        return await self.observe()

    async def observe(self, *, include_text: bool = True) -> PageObservation:
        if self._closed:
            return self._closed_observation("")
        page = self._page
        url = page.url
        title = await page.title()
        text = await page.inner_text("body") if include_text else None
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
        if self._closed:
            raise session_closed_error(self._session_id)
        elements = await self._extract_controls()
        if role is not None:
            elements = [el for el in elements if el.role is role]
        return elements

    async def _fill_radio(self, locator: str, value: str, start: float) -> ActionResult:
        first = self._page.locator(locator).first
        name = await first.get_attribute("name")
        form_index = await first.evaluate(
            "el => el.form ? Array.from(document.forms).indexOf(el.form) : -1"
        )
        group = (
            self._page.locator(f'input[type="radio"]{_css_attr("name", name)}')
            if name
            else self._page.locator(locator)
        )
        count = await group.count()
        for index in range(count):
            radio = group.nth(index)
            radio_form_index = await radio.evaluate(
                "el => el.form ? Array.from(document.forms).indexOf(el.form) : -1"
            )
            if radio_form_index != form_index:
                continue
            tag = await radio.evaluate("el => el.tagName")
            if tag != "INPUT" or not await radio.is_enabled():
                continue
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

    async def _fill_checkbox(self, locator: str, value: str, start: float) -> ActionResult:
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

    async def fill_field(self, locator: str, value: str) -> ActionResult:
        start = time.perf_counter()
        try:
            target = self._page.locator(locator).first
            input_type = (await target.get_attribute("type") or "").lower()
            aria_role = (await target.get_attribute("role") or "").lower()
            tag = await target.evaluate("el => el.tagName")
            unsupported = _unsupported_aria_fill(tag, input_type, aria_role, start)
            if unsupported is not None:
                return unsupported
            if input_type == "radio" or aria_role == "radio":
                return await self._fill_radio(locator, value, start)
            if input_type == "checkbox" or aria_role == "checkbox":
                return await self._fill_checkbox(locator, value, start)
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
        if self._closed:
            raise session_closed_error(self._session_id)
        full_path = self._settings.artifacts_dir / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=str(full_path))
        return relative_path

    # -- tabs -------------------------------------------------------------

    async def list_tabs(self) -> list[BrowserTab]:
        if self._closed:
            raise session_closed_error(self._session_id)
        active = self._resolve_active_page()
        return [
            BrowserTab(
                id=self._tab_ids[page],
                url=page.url,
                title=await self._safe_title(page),
                active=page is active,
            )
            for page in self._track_pages()
        ]

    async def open_tab(self, url: str | None = None) -> BrowserTab:
        if self._closed:
            raise session_closed_error(self._session_id)
        page = await self._context.new_page()
        if url:
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception:
                # The caller never received this tab. Leaving it in the context
                # would make a later list_tabs report a blank page nobody asked
                # for, while the exception said the open failed.
                with contextlib.suppress(Exception):
                    await page.close()
                raise
        # A caller that just asked for a tab means to work in it. Leaving the
        # previous tab active would send the next fill to the wrong page, and
        # the caller would have no signal that it had happened.
        self._active_page = page
        self._track_pages()
        return BrowserTab(
            id=self._tab_ids[page],
            url=page.url,
            title=await self._safe_title(page),
            active=True,
        )

    def _page_for(self, tab_id: str) -> Any | None:
        self._track_pages()
        return next((page for page, known in self._tab_ids.items() if known == tab_id), None)

    async def activate_tab(self, tab_id: str) -> ActionResult:
        if self._closed:
            return self._closed_result("activate_tab")
        page = self._page_for(tab_id)
        if page is None:
            return ActionResult(
                ok=False, action="activate_tab", detail=f"no tab {tab_id!r} in this session"
            )
        self._active_page = page
        with contextlib.suppress(Exception):
            # Window ordering, which only exists when headed. Failing an
            # activation because a headless context has no window to raise
            # would break the operation over its cosmetic half.
            await page.bring_to_front()
        return ActionResult(ok=True, action="activate_tab", detail=tab_id)

    async def close_tab(self, tab_id: str) -> ActionResult:
        if self._closed:
            return self._closed_result("close_tab")
        page = self._page_for(tab_id)
        if page is None:
            return ActionResult(
                ok=False, action="close_tab", detail=f"no tab {tab_id!r} in this session"
            )
        await page.close()
        self._tab_ids.pop(page, None)
        # Closing the active tab picks the first tab the context still lists,
        # and opens a blank one when it lists none. A session left pointing at a
        # closed page fails every later operation with a Playwright error that
        # names neither the tab nor the session.
        if self._resolve_active_page() is None:
            self._active_page = await self._context.new_page()
        self._track_pages()
        return ActionResult(ok=True, action="close_tab", detail=tab_id)

    # -- downloads --------------------------------------------------------

    async def download(
        self, locator: str, *, timeout_seconds: float | None = None
    ) -> BrowserDownload:
        """Click ``locator`` while listening for the file it sends back.

        The listener has to be installed before the click, because Chromium
        surfaces the download as an event during navigation; polling afterwards
        finds nothing.
        """
        if self._closed:
            raise session_closed_error(self._session_id)
        page = self._page
        timeout = timeout_seconds or self._settings.browser.navigation_timeout_seconds
        try:
            async with page.expect_download(timeout=int(timeout * 1000)) as pending:
                await page.click(locator)
            handle = await pending.value
        except Exception as exc:
            raise ApplyuminatiError(
                f"clicking {locator!r} produced no download",
                code="browser.no_download",
                category=FailureCategory.EXTRACTION_DRIFT,
                details={"locator": locator, "detail": str(exc)[:300]},
            ) from exc

        root = self._settings.downloads_dir
        # Session ids are caller-supplied. Validate the directory against the
        # downloads root *before* mkdir, otherwise ``../../outside`` creates
        # the outside path and the later check only notices after the fact.
        directory = ensure_download_directory(root, root / self._session_id)
        suggested = handle.suggested_filename or None
        destination = resolve_download_path(root, directory, suggested)
        await handle.save_as(str(destination))
        return BrowserDownload(
            filename=destination.name,
            relative_path=destination.relative_to(root.resolve()).as_posix(),
            suggested_filename=suggested,
            # Playwright does not report a content type for a download, and a
            # type guessed from the extension is indistinguishable to the caller
            # from one the server actually sent.
            mime_type=None,
            size=destination.stat().st_size if destination.is_file() else None,
            source_url=handle.url or None,
        )

    async def checkpoint(self) -> BrowserCheckpoint:
        if self._closed:
            raise session_closed_error(self._session_id)
        page = self._resolve_active_page()
        storage_configured = self._settings.browser.playwright_storage_state is not None
        return BrowserCheckpoint(
            session_id=self._session_id,
            url=page.url if page is not None else "",
            backend_state={
                "type": "playwright",
                # Spelled out because a checkpoint is easy to read as a saved
                # browser, and this one is not. Resuming opens a *new* context,
                # loads the cookie jar when one is configured, and navigates to
                # this url. Open tabs, DOM state, in-page JavaScript and the
                # back/forward stack are gone, and no amount of reading this
                # record brings them back. Ego lite task spaces are what
                # PERSISTENT_SESSION means; this is not that.
                #
                # The configured storage-state *path* is not recorded. Resume
                # already reads it from current settings, and persisting the
                # host path would leak it into attempt state, logs, and APIs.
                "restores": ["url", *(["storage_state"] if storage_configured else [])],
            },
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
        """Close this session's context, and only this session's context.

        The browser is shared, so it is deliberately absent from everything
        below. Idempotent: a second call returns immediately rather than
        reaching for a context that is already gone.
        """
        if self._closed:
            return
        self._closed = True
        await self._save_storage_state()
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                log.debug("playwright.context_close_failed", exc_info=True)
        self._tab_ids.clear()
        self._active_page = None
        if self._on_close is not None:
            self._on_close(self)

    async def _save_storage_state(self) -> None:
        """Persist cookies so the PERSISTENT_LOGIN claim is actually true.

        Delegates to the backend-owned :class:`StorageStateStore`. A stale
        writer skips rather than clobbering a newer generation; that skip is a
        persistence conflict, not a close failure.
        """
        if self._store is None or self._context is None:
            return
        try:
            await self._store.commit(
                self._context,
                loaded_generation=self._loaded_storage_generation,
                session_id=self._session_id,
            )
        except Exception:
            log.warning(
                "playwright.storage_state_not_saved",
                session_id=self._session_id,
                loaded_generation=self._loaded_storage_generation,
            )

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
    """Owns the Playwright runtime and the browser process. Sessions own contexts.

    One browser, many contexts. A context is cheap and already isolated —
    separate cookies, separate storage, separate pages — which is exactly what a
    session needs, while a browser per session would multiply a concurrent run's
    memory by a Chromium process each.

    The ownership split is the point of this class. Nothing below it may close
    the browser, and :meth:`aclose` is the only thing that does.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._sessions: dict[str, PlaywrightSession] = {}
        self._shutdown = False
        path = settings.playwright_storage_state_path
        self._storage_store = StorageStateStore(path) if path is not None else None
        #: Serialises the launch, so two sessions opening at once start one
        #: browser rather than two, of which one would be orphaned.
        self._launching = asyncio.Lock()
        #: Serialises the duplicate-id check through registration. The check
        #: alone is not enough: two concurrent opens of the same id both pass
        #: it, then the later assignment orphans the first context.
        self._opening = asyncio.Lock()

    @property
    def metadata(self) -> BrowserMetadata:
        return _metadata(self._settings)

    @property
    def browser_running(self) -> bool:
        """Whether a live browser process is currently attached."""
        browser = self._browser
        return browser is not None and bool(browser.is_connected())

    @property
    def live_session_ids(self) -> tuple[str, ...]:
        """Sessions still holding a context. Ordered by when they opened."""
        return tuple(self._sessions)

    async def health(self) -> HealthReport:
        facts = self._persistence_facts()
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
                facts=facts,
            )
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(**_launch_options(self._settings))
                await browser.close()
            readable = bool(facts["persistence_readable"])
            if readable:
                return HealthReport(
                    plugin="playwright",
                    state=HealthState.HEALTHY,
                    detail="Chromium is installed and launchable",
                    facts=facts,
                )
            return HealthReport(
                plugin="playwright",
                state=HealthState.DEGRADED,
                detail=(
                    "Chromium is installed and launchable; persisted login state is unreadable"
                ),
                facts=facts,
            )
        except BackendUnavailableError as exc:
            # A launch option is misconfigured (missing custom executable); the
            # backend itself may be installed and fine once reconfigured.
            return HealthReport(
                plugin="playwright",
                state=HealthState.UNAVAILABLE,
                detail=str(exc),
                facts=facts,
            )
        except Exception as exc:
            return HealthReport(
                plugin="playwright",
                state=HealthState.NOT_INSTALLED,
                detail=f"Chromium not found; run `playwright install chromium`. Detail: {exc}",
                facts=facts,
            )

    def _persistence_facts(self) -> dict[str, object]:
        facts: dict[str, object] = {
            "persistence_configured": self._storage_store is not None,
            "persistence_state_exists": False,
            "persistence_readable": True,
            "persistence_generation": 0,
            "proxy_configured": self._settings.browser.playwright_proxy is not None,
            "channel_configured": self._settings.browser.playwright_channel is not None,
            "custom_executable_configured": (
                self._settings.browser.playwright_executable_path is not None
            ),
        }
        if self._storage_store is not None:
            status = self._storage_store.status()
            facts["persistence_state_exists"] = status.state_exists
            facts["persistence_readable"] = status.readable
            facts["persistence_generation"] = status.generation
        return facts

    async def open_session(
        self,
        *,
        session_id: str | None = None,
        resume: BrowserCheckpoint | None = None,
        task_space: str | None = None,
    ) -> BrowserSession:
        """Open one isolated context, optionally re-entering a checkpoint's url.

        ``resume`` is honoured only as far as it honestly can be. A new context
        is created, the configured cookie jar is loaded into it, and the
        checkpoint's url is opened. Tabs, DOM state, in-page JavaScript and
        history are not restored and cannot be: nothing recorded them, because
        a Playwright context does not survive the process that made it. That is
        the same reason this backend does not advertise PERSISTENT_SESSION.
        """
        # Playwright has no workspace that outlives the context, so it cannot
        # honour a task-space name either.
        _ = task_space
        sid = session_id or (resume.session_id if resume else None) or new_ulid()
        async with self._opening:
            if self._shutdown:
                raise self._closed_error()
            if sid in self._sessions:
                # Checked before a context is built, so a retried create_session
                # neither launches a second context nor displaces the first. Two
                # live sessions under one id is not a state to reconcile: the
                # displaced one would be unreachable, and its later close would
                # evict the survivor from the registry. Held under ``_opening``
                # so two concurrent opens cannot both pass the check.
                raise DuplicateActionError(
                    f"session {sid!r} is already open on this backend; "
                    "close it before opening another with the same id",
                    code="browser.session_id_in_use",
                    details={"session_id": sid, "backend": "playwright"},
                )

            snapshot_path: str | None = None
            loaded_generation = 0
            if self._storage_store is not None:
                snapshot = await self._storage_store.load()
                snapshot_path = str(snapshot.path) if snapshot.path is not None else None
                loaded_generation = snapshot.generation

            browser = await self._ensure_browser()
            context_kwargs: dict[str, object] = {}
            if snapshot_path is not None:
                context_kwargs["storage_state"] = snapshot_path
            context = await browser.new_context(**context_kwargs)
            await context.new_page()
            session = PlaywrightSession(
                context,
                session_id=sid,
                settings=self._settings,
                on_close=self._forget,
                store=self._storage_store,
                loaded_storage_generation=loaded_generation,
            )
            self._sessions[sid] = session
        if resume is not None and resume.url:
            try:
                await session.navigate(resume.url)
            except Exception as exc:
                # The caller never received this session, so nothing else will
                # close it. Leaving it registered would hold a context until the
                # whole backend shut down.
                await session.close()
                if self._shutdown:
                    # aclose() tore the page down mid-goto. The Playwright
                    # ERR_ABORTED is real but names neither the session nor the
                    # backend; the closed-backend error does.
                    raise self._closed_error() from exc
                raise
        return await self._require_live(session)

    async def _require_live(self, session: PlaywrightSession) -> PlaywrightSession:
        """Refuse to hand back a session that shutdown already took.

        Resume navigation sits outside ``_opening`` so other opens are not
        blocked on it. ``aclose`` can therefore close this session after
        registration and before we return. ``navigate`` turns a closed session
        into a ``PageObservation``, which would look like a successful open of
        a dead session. Under the lock, that case is a closed backend.
        """
        async with self._opening:
            if self._shutdown or session.closed:
                if not session.closed:
                    await session.close()
                raise self._closed_error()
            return session

    def _forget(self, session: PlaywrightSession) -> None:
        """Drop a session from the registry, if it is still the one registered.

        Identity-checked rather than keyed by id alone. Popping by id would let
        a late close from a session that had already been replaced evict its
        replacement, and the survivor's context would then be missed by
        :meth:`aclose`.
        """
        if self._sessions.get(session.session_id) is session:
            del self._sessions[session.session_id]

    def _closed_error(self) -> BackendUnavailableError:
        return BackendUnavailableError(
            "the playwright backend has been closed; construct a new one to open sessions",
            code="browser.backend_closed",
            details={"backend": "playwright"},
        )

    async def _ensure_browser(self) -> Any:
        from playwright.async_api import async_playwright

        async with self._launching:
            if self._shutdown:
                raise self._closed_error()
            if self._browser is not None and not self._browser.is_connected():
                # The process died under us: an OOM kill, a crash. Relaunching
                # is right; handing the dead handle to a new session is not.
                log.warning("playwright.browser_disconnected")
                self._browser = None
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            if self._browser is None:
                self._browser = await self._playwright.chromium.launch(
                    **_launch_options(self._settings)
                )
            return self._browser

    async def aclose(self) -> None:
        """Close remaining contexts, then the browser, then the runtime.

        Ordered, because closing the browser first would leave each session
        writing its storage state through a dead connection. Idempotent, and
        permanent: a closed backend refuses to open further sessions rather than
        quietly launching a second browser nobody is tracking.

        Takes ``_opening`` so shutdown cannot interleave with construction or
        with the live-session handoff at the end of :meth:`open_session`.
        """
        async with self._opening:
            self._shutdown = True
            for session in list(self._sessions.values()):
                try:
                    await session.close()
                except Exception:
                    log.debug("playwright.session_close_failed", exc_info=True)
            self._sessions.clear()
            if self._browser is not None:
                with contextlib.suppress(Exception):
                    await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
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
