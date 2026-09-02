"""Native Browser Host: TLS requirement, dispatcher refusals, advertisement."""

from __future__ import annotations

from pathlib import Path

import pytest

from applyuminati.browser.base import (
    ActionResult,
    BrowserCapability,
    BrowserCheckpoint,
    BrowserDownload,
    BrowserTab,
    ControlOwner,
    ElementRole,
    PageElement,
    PageObservation,
)
from applyuminati.browser.host_protocol import CommandMessage, HostCommand, HostErrorCode
from applyuminati.core.errors import ConfigurationError
from applyuminati.host.dispatcher import (
    HOST_UNDISPATCHABLE_CAPABILITIES,
    CommandDispatcher,
    HostSession,
    host_advertised_capabilities,
)
from applyuminati.host.security import require_secure_server


class _Session:
    session_id = "s1"
    owner = ControlOwner.AGENT
    task_space_id = "applyuminati:att1"

    async def control_state(self) -> ControlOwner:
        return ControlOwner.AGENT

    async def navigate(self, url: str, *, wait_for_load: bool = True) -> PageObservation:
        return PageObservation(url=url, title="ok")

    async def observe(self, *, include_text: bool = True) -> PageObservation:
        return PageObservation(url="https://example.com")

    async def find_controls(self, *, role: ElementRole | None = None) -> list[PageElement]:
        return []

    async def wait_for_control(self, *, timeout_seconds: float) -> ActionResult:
        return ActionResult(ok=True, action="wait")

    async def fill_field(self, locator: str, value: str) -> ActionResult:
        return ActionResult(ok=True, action="fill")

    async def click(
        self,
        locator: str,
        *,
        label: str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        _ = (label, idempotency_key)
        return ActionResult(ok=True, action="click")

    async def select_option(self, locator: str, option: str) -> ActionResult:
        return ActionResult(ok=True, action="select")

    async def set_checked(self, locator: str, checked: bool) -> ActionResult:
        return ActionResult(ok=True, action="check")

    async def upload_file(self, locator: str, path: Path) -> ActionResult:
        return ActionResult(ok=True, action="upload")

    async def wait_for_navigation(self, *, timeout_seconds: float | None = None) -> ActionResult:
        return ActionResult(ok=True, action="wait")

    async def screenshot(self, *, relative_path: str) -> str:
        return relative_path

    # Implemented, unlike the drivers' fake, so the dispatcher tests below prove
    # the host refuses tab and download commands on its own account rather than
    # because the session behind it happened to be incapable.

    async def list_tabs(self) -> list[BrowserTab]:
        return [BrowserTab(id="tab-1", url="https://example.com", active=True)]

    async def open_tab(self, url: str | None = None) -> BrowserTab:
        return BrowserTab(id="tab-2", url=url or "about:blank", active=True)

    async def activate_tab(self, tab_id: str) -> ActionResult:
        return ActionResult(ok=True, action="activate_tab", detail=tab_id)

    async def close_tab(self, tab_id: str) -> ActionResult:
        return ActionResult(ok=True, action="close_tab", detail=tab_id)

    async def download(
        self, locator: str, *, timeout_seconds: float | None = None
    ) -> BrowserDownload:
        return BrowserDownload(filename="offer.pdf", relative_path="s1/offer.pdf")

    async def checkpoint(self) -> BrowserCheckpoint:
        return BrowserCheckpoint(session_id=self.session_id, url="https://example.com")

    async def request_human_control(self, instruction: str) -> ActionResult:
        return ActionResult(ok=True, action="handoff")

    async def reclaim_control(self, *, confirmed_by_user: bool) -> ActionResult:
        return ActionResult(ok=confirmed_by_user, action="reclaim")

    async def close(self) -> None:
        return None


def test_remote_plaintext_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        require_secure_server("ws://nas.local/api/v1/browser-hosts/ws", allow_insecure=False)


def test_loopback_plaintext_is_allowed() -> None:
    require_secure_server("ws://127.0.0.1:8000/api/v1/browser-hosts/ws", allow_insecure=False)


def test_insecure_override_is_explicit() -> None:
    require_secure_server("ws://nas.local/api/v1/browser-hosts/ws", allow_insecure=True)


async def test_upload_outside_documents_dir_is_refused(tmp_path: Path) -> None:
    dispatcher = CommandDispatcher(documents_dir=tmp_path / "docs", capabilities=frozenset())
    (tmp_path / "docs").mkdir()
    hosted = HostSession(_Session(), "playwright")
    result = await dispatcher.execute(
        hosted,
        CommandMessage(
            command=HostCommand.UPLOAD,
            session_id="s1",
            params={"locator": "f", "path": "/etc/passwd"},
        ),
    )
    assert result.ok is False
    assert result.error_code is HostErrorCode.MALFORMED


async def test_evaluate_is_not_host_scoped_script(tmp_path: Path) -> None:
    dispatcher = CommandDispatcher(
        documents_dir=tmp_path, capabilities=frozenset({"javascript_eval"})
    )
    result = await dispatcher.execute(
        HostSession(_Session(), "ego_lite"),
        CommandMessage(command=HostCommand.EVALUATE, session_id="s1", params={"expression": "1"}),
    )
    assert result.ok is False
    assert result.error_code is HostErrorCode.UNKNOWN_COMMAND


async def test_expired_commands_are_not_executed(tmp_path: Path) -> None:
    dispatcher = CommandDispatcher(documents_dir=tmp_path, capabilities=frozenset())
    result = await dispatcher.execute(
        HostSession(_Session(), "playwright"),
        CommandMessage(
            command=HostCommand.CLICK, session_id="s1", expires_at=0, params={"locator": "x"}
        ),
    )
    assert result.error_code is HostErrorCode.EXPIRED


def test_host_advertisement_drops_capabilities_the_dispatcher_cannot_run() -> None:
    advertised = host_advertised_capabilities(
        {
            BrowserCapability.NAVIGATE,
            BrowserCapability.HUMAN_HANDOFF,
            BrowserCapability.JAVASCRIPT_EVAL,
            BrowserCapability.MULTI_TAB,
            BrowserCapability.DOWNLOADS,
        }
    )
    assert BrowserCapability.NAVIGATE.value in advertised
    assert BrowserCapability.HUMAN_HANDOFF.value in advertised
    assert BrowserCapability.JAVASCRIPT_EVAL.value not in advertised
    assert BrowserCapability.MULTI_TAB.value not in advertised
    assert BrowserCapability.DOWNLOADS.value not in advertised
    assert {
        BrowserCapability.JAVASCRIPT_EVAL,
        BrowserCapability.MULTI_TAB,
        BrowserCapability.DOWNLOADS,
    } <= HOST_UNDISPATCHABLE_CAPABILITIES


async def test_advertise_backends_never_promises_undispatchable_operations() -> None:
    from applyuminati.core.settings import Settings
    from applyuminati.host.discovery import advertise_backends

    advertised = await advertise_backends(Settings())
    forbidden = {item.value for item in HOST_UNDISPATCHABLE_CAPABILITIES}
    for advertisement in advertised.values():
        overlap = set(advertisement.capabilities) & forbidden
        assert overlap == set(), f"{advertisement} advertised {overlap}"


@pytest.mark.parametrize(
    ("command", "capability"),
    [
        (HostCommand.LIST_TABS, BrowserCapability.MULTI_TAB),
        (HostCommand.OPEN_TAB, BrowserCapability.MULTI_TAB),
        (HostCommand.ACTIVATE_TAB, BrowserCapability.MULTI_TAB),
        (HostCommand.CLOSE_TAB, BrowserCapability.MULTI_TAB),
        (HostCommand.DOWNLOAD, BrowserCapability.DOWNLOADS),
    ],
)
async def test_tab_and_download_commands_are_refused_by_capability_name(
    tmp_path: Path, command: HostCommand, capability: BrowserCapability
) -> None:
    """The local session implements these; the host still does not dispatch them.

    A backend being able to do something in-process is not the same claim as a
    host being able to do it over the wire, and the two are advertised
    separately. ``_Session`` above implements all five, so a refusal here can
    only come from the dispatcher.
    """
    dispatcher = CommandDispatcher(
        documents_dir=tmp_path,
        capabilities=frozenset({BrowserCapability.MULTI_TAB.value, "downloads"}),
    )
    result = await dispatcher.execute(
        HostSession(_Session(), "playwright"),
        CommandMessage(command=command, session_id="s1", params={"tab_id": "tab-1"}),
    )
    assert result.ok is False
    assert result.error_code is HostErrorCode.CAPABILITY_UNAVAILABLE
    assert capability.value in (result.error_message or "")


def test_a_backend_that_does_tabs_locally_still_does_not_advertise_them_remotely() -> None:
    """Playwright gained MULTI_TAB and DOWNLOADS. The host must not inherit them."""
    from applyuminati.core.settings import Settings
    from applyuminati.plugins.browsers.playwright_backend import PlaywrightBackend

    local = PlaywrightBackend(Settings()).metadata.capabilities
    assert BrowserCapability.MULTI_TAB in local
    assert BrowserCapability.DOWNLOADS in local

    advertised = host_advertised_capabilities(local)
    assert BrowserCapability.MULTI_TAB.value not in advertised
    assert BrowserCapability.DOWNLOADS.value not in advertised
    assert BrowserCapability.NAVIGATE.value in advertised


async def test_consequential_click_is_deduplicated(tmp_path: Path) -> None:
    dispatcher = CommandDispatcher(documents_dir=tmp_path, capabilities=frozenset())
    hosted = HostSession(_Session(), "playwright")
    first = await dispatcher.execute(
        hosted,
        CommandMessage(
            command=HostCommand.CLICK,
            session_id="s1",
            params={"locator": "submit"},
            idempotency_key="submit-1",
        ),
    )
    second = await dispatcher.execute(
        hosted,
        CommandMessage(
            command=HostCommand.CLICK,
            session_id="s1",
            params={"locator": "submit"},
            idempotency_key="submit-1",
        ),
    )
    assert first.ok
    assert second.ok
    assert second.deduplicated is True
