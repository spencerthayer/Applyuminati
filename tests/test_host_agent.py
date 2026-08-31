"""Native Browser Host: TLS requirement, dispatcher refusals, advertisement."""

from __future__ import annotations

from pathlib import Path

import pytest

from applyuminati.browser.base import (
    ActionResult,
    BrowserCheckpoint,
    ControlOwner,
    ElementRole,
    PageElement,
    PageObservation,
)
from applyuminati.browser.host_protocol import CommandMessage, HostCommand, HostErrorCode
from applyuminati.core.errors import ConfigurationError
from applyuminati.host.dispatcher import CommandDispatcher, HostSession
from applyuminati.host.security import require_secure_server


class _Session:
    session_id = "s1"
    owner = ControlOwner.AGENT

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

    async def click(self, locator: str, *, label: str | None = None) -> ActionResult:
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
