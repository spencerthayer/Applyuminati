"""Playwright browser backend: the portable fallback.

Playwright is imported lazily inside functions so the package stays importable
without it installed. ``health()`` returns ``NOT_INSTALLED`` with an actionable
message when the import fails or browsers are not downloaded.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
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
    data_testid: str | None = None
    aria_role: str | None = None
    index_in_type: int = 0


_CSS_ID_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_NATIVE_NTH_TAGS = frozenset({"input", "textarea", "select", "button", "a"})


def _css_attr(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{name}="{escaped}"]'


def _nth_group(control: PlaywrightControl) -> str:
    if control.input_type == "contenteditable":
        return "contenteditable"
    if control.aria_role:
        return f"role:{control.aria_role}"
    if control.tag == "input" and control.input_type:
        return f"input:{control.input_type}"
    if control.tag == "a":
        return "a[href]"
    if control.tag in _NATIVE_NTH_TAGS:
        return control.tag
    return control.tag


def _nth_selector(control: PlaywrightControl) -> str:
    """Selector whose nth index is counted only among matching nodes."""
    n = control.index_in_type
    if control.input_type == "contenteditable":
        return f":is([contenteditable='true'], [contenteditable='']) >> nth={n}"
    if control.aria_role:
        escaped = control.aria_role.replace("'", "\\'")
        return f"[role='{escaped}'] >> nth={n}"
    if control.tag == "input" and control.input_type:
        escaped = control.input_type.replace("'", "\\'")
        return f"input[type='{escaped}'] >> nth={n}"
    if control.tag == "a":
        return f"a[href] >> nth={n}"
    if control.tag in _NATIVE_NTH_TAGS:
        return f"{control.tag} >> nth={n}"
    return f"{control.tag} >> nth={n}"


def build_playwright_locator(control: PlaywrightControl, *, used: set[str]) -> str:
    """Return a unique Playwright selector string for one control."""
    candidates: list[str] = []
    if control.element_id:
        if _CSS_ID_RE.match(control.element_id):
            candidates.append(f"#{control.element_id}")
        else:
            candidates.append(_css_attr("id", control.element_id))
    if control.input_type == "radio" and control.name and control.value is not None:
        candidates.append(_css_attr("name", control.name) + _css_attr("value", control.value))
    elif control.name:
        candidates.append(_css_attr("name", control.name))
    if control.aria_label:
        candidates.append(_css_attr("aria-label", control.aria_label))
    if control.placeholder:
        candidates.append(_css_attr("placeholder", control.placeholder))
    if control.data_testid:
        candidates.append(_css_attr("data-testid", control.data_testid))
    candidates.append(_nth_selector(control))
    for candidate in candidates:
        if candidate not in used:
            return candidate
    return _nth_selector(control)


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
  const seen = new Set();
  const rows = [];
  document.querySelectorAll(selector).forEach((el) => {
    if (seen.has(el)) return;
    seen.add(el);
    rows.push({
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || (el.tagName === 'INPUT' ? 'text' : null)),
      id: el.id || null,
      name: el.getAttribute('name'),
      value: el.getAttribute('value'),
      placeholder: el.getAttribute('placeholder'),
      accessibleName: accessibleName(el),
      ariaLabel: (el.getAttribute('aria-label') || '').trim() || null,
      ariaRole: el.getAttribute('role'),
      dataTestid: el.getAttribute('data-testid') || el.getAttribute('data-qa'),
      required: el.hasAttribute('required'),
      disabled: el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true',
      contenteditable: el.isContentEditable && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA',
      options: optionsFor(el)
    });
  });
  return rows;
}"""


_BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})


def _classify_control(row: dict[str, Any]) -> tuple[ElementRole, str | None]:
    tag = str(row.get("tag") or "")
    raw_type = row.get("type")
    input_type = str(raw_type).lower() if raw_type else None
    aria_role = (str(row.get("ariaRole") or "")).lower() or None
    role = ElementRole.TEXTBOX
    if row.get("contenteditable"):
        return ElementRole.TEXTBOX, "contenteditable"
    if tag == "select" or (aria_role == "combobox" and tag not in {"input", "textarea"}):
        role = ElementRole.SELECT
    elif tag == "textarea":
        role = ElementRole.TEXTAREA
    elif tag == "button" or input_type in _BUTTON_INPUT_TYPES:
        role = ElementRole.BUTTON
    elif tag == "a" or aria_role == "link":
        role = ElementRole.LINK
    elif input_type == "file":
        role = ElementRole.FILE_INPUT
        input_type = "file"
    elif input_type == "checkbox" or aria_role == "checkbox":
        role = ElementRole.CHECKBOX
        input_type = input_type or "checkbox"
    elif input_type == "radio" or aria_role == "radio":
        role = ElementRole.RADIO
        input_type = input_type or "radio"
    elif aria_role == "combobox":
        input_type = input_type or "combobox"
    elif aria_role == "button":
        role = ElementRole.BUTTON
    else:
        input_type = input_type or ("text" if tag == "input" else None)
    return role, input_type


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def elements_from_metadata(rows: Sequence[Any]) -> list[PageElement]:
    """Turn Playwright metadata rows into uniquely addressed page elements."""
    used: set[str] = set()
    group_counts: dict[str, int] = {}
    elements: list[PageElement] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        role, input_type = _classify_control(row)
        tag = str(row.get("tag") or "")
        aria_role = _optional_str(row.get("ariaRole"))
        if aria_role:
            aria_role = aria_role.lower()
        staged = PlaywrightControl(
            tag=tag,
            input_type=input_type,
            element_id=_optional_str(row.get("id")),
            name=_optional_str(row.get("name")),
            value=None if row.get("value") is None else str(row.get("value")),
            aria_label=_optional_str(row.get("ariaLabel")),
            placeholder=_optional_str(row.get("placeholder")),
            data_testid=_optional_str(row.get("dataTestid")),
            aria_role=aria_role,
        )
        group = _nth_group(staged)
        index = group_counts.get(group, 0)
        group_counts[group] = index + 1
        control = replace(staged, index_in_type=index)
        locator = build_playwright_locator(control, used=used)
        used.add(locator)
        accessible = _optional_str(row.get("accessibleName")) or control.aria_label
        options_raw = row.get("options") or []
        options = (
            [str(item) for item in options_raw if item] if isinstance(options_raw, list) else []
        )
        elements.append(
            PageElement(
                locator=locator,
                role=role,
                label=accessible,
                name=control.name,
                value=control.value,
                placeholder=control.placeholder,
                required=bool(row.get("required")),
                disabled=bool(row.get("disabled")),
                options=options,
                input_type=input_type,
            )
        )
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
            self._page.locator(f'input[type="radio"]{_css_attr("name", name)}')
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
            input_type = await self._page.locator(locator).first.get_attribute("type")
            if (input_type or "").lower() == "radio":
                return await self._fill_radio(locator, value, start)
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
