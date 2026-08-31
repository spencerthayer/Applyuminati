"""Playwright browser backend: the portable fallback.

Playwright is imported lazily inside functions so the package stays importable
without it installed. ``health()`` returns ``NOT_INSTALLED`` with an actionable
message when the import fails or browsers are not downloaded.
"""

from __future__ import annotations

import time
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
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import Settings

log = get_logger(__name__)

__all__ = ["PLUGIN", "PlaywrightBackend", "PlaywrightSession"]

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

    async def navigate(self, url: str, *, wait_for_load: bool = True) -> PageObservation:
        await self._page.goto(url, wait_until="domcontentloaded" if wait_for_load else "commit")
        return await self.observe()

    async def observe(self, *, include_text: bool = True) -> PageObservation:
        url = self._page.url
        title = await self._page.title()
        text = await self._page.inner_text("body") if include_text else None
        elements = await self._extract_controls()
        condition = self._detect_condition(url, text or "")
        return PageObservation(
            url=url,
            title=title,
            text=text,
            elements=elements,
            condition=condition,
        )

    async def find_controls(self, *, role: ElementRole | None = None) -> list[PageElement]:
        elements = await self._extract_controls()
        if role is not None:
            elements = [el for el in elements if el.role is role]
        return elements

    async def fill_field(self, locator: str, value: str) -> ActionResult:
        start = time.perf_counter()
        try:
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

    async def click(self, locator: str, *, label: str | None = None) -> ActionResult:
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
        elements: list[PageElement] = []
        for selector, role in [
            ("input[type='text']", ElementRole.TEXTBOX),
            ("input[type='email']", ElementRole.TEXTBOX),
            ("textarea", ElementRole.TEXTAREA),
            ("select", ElementRole.SELECT),
            ("input[type='checkbox']", ElementRole.CHECKBOX),
            ("input[type='radio']", ElementRole.RADIO),
            ("button", ElementRole.BUTTON),
            ("a", ElementRole.LINK),
            ("input[type='file']", ElementRole.FILE_INPUT),
        ]:
            handles = await self._page.query_selector_all(selector)
            for handle in handles:
                label = (
                    await handle.get_attribute("aria-label")
                    or await handle.get_attribute("name")
                    or await handle.get_attribute("placeholder")
                )
                name = await handle.get_attribute("name")
                value = await handle.get_attribute("value")
                required = await handle.get_attribute("required") is not None
                disabled = await handle.get_attribute("disabled") is not None
                placeholder = await handle.get_attribute("placeholder")
                options: list[str] = []
                if role is ElementRole.SELECT:
                    option_handles = await handle.query_selector_all("option")
                    for opt in option_handles:
                        text = await opt.inner_text()
                        if text:
                            options.append(text.strip())
                elements.append(
                    PageElement(
                        locator=selector,
                        role=role,
                        label=label,
                        name=name,
                        value=value,
                        placeholder=placeholder,
                        required=required,
                        disabled=disabled,
                        options=options,
                    )
                )
        return elements

    @staticmethod
    def _detect_condition(url: str, text: str) -> PageCondition:
        lowered = text.lower()[:5000]
        if any(marker in lowered for marker in ["captcha", "are you a robot", "please verify"]):
            return PageCondition.HUMAN_CHALLENGE
        if any(
            marker in lowered for marker in ["sign in", "log in", "please log in", "login required"]
        ):
            return PageCondition.LOGIN_REQUIRED
        if any(marker in lowered for marker in ["access denied", "blocked", "bot detection"]):
            return PageCondition.AUTOMATION_BLOCKED
        if "404" in lowered or "not found" in lowered:
            return PageCondition.NOT_FOUND
        return PageCondition.OK


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
        self, *, session_id: str | None = None, resume: BrowserCheckpoint | None = None
    ) -> BrowserSession:
        from playwright.async_api import async_playwright

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
)
