"""BrowserSession that forwards semantic commands to a live Browser Host."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic

from applyuminati.browser.base import (
    ActionResult,
    BrowserCheckpoint,
    ControlOwner,
    ElementRole,
    PageElement,
    PageObservation,
)
from applyuminati.browser.host_manager import BrowserHostManager, HostCommandError
from applyuminati.browser.host_protocol import HostCommand

__all__ = ["HostedBrowserSession"]

#: Gap between ownership polls while waiting for a human to hand control back.
#: The host pushes a control_changed event too, but a poll is what a caller
#: holding a deadline can rely on.
_CONTROL_POLL_SECONDS = 2.0


class HostedBrowserSession:
    """One remote browsing context addressed through :class:`BrowserHostManager`."""

    def __init__(self, manager: BrowserHostManager, host_id: str, session_id: str) -> None:
        self._manager = manager
        self._host_id = host_id
        self.session_id = session_id
        self._owner = ControlOwner.AGENT

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    async def navigate(self, url: str, *, wait_for_load: bool = True) -> PageObservation:
        payload = await self._ok(
            HostCommand.NAVIGATE,
            {"url": url, "wait_for_load": wait_for_load},
        )
        return PageObservation.model_validate(payload)

    async def observe(self, *, include_text: bool = True) -> PageObservation:
        payload = await self._ok(HostCommand.OBSERVE, {"include_text": include_text})
        return PageObservation.model_validate(payload)

    async def find_controls(self, *, role: ElementRole | None = None) -> list[PageElement]:
        observation = await self.observe()
        if role is None:
            return list(observation.elements)
        return [element for element in observation.elements if element.role is role]

    async def fill_field(self, locator: str, value: str) -> ActionResult:
        payload = await self._ok(HostCommand.FILL, {"locator": locator, "value": value})
        return ActionResult.model_validate(payload)

    async def select_option(self, locator: str, option: str) -> ActionResult:
        payload = await self._ok(HostCommand.SELECT, {"locator": locator, "option": option})
        return ActionResult.model_validate(payload)

    async def set_checked(self, locator: str, checked: bool) -> ActionResult:
        payload = await self._ok(HostCommand.SET_CHECKED, {"locator": locator, "checked": checked})
        return ActionResult.model_validate(payload)

    async def upload_file(self, locator: str, path: Path) -> ActionResult:
        payload = await self._ok(HostCommand.UPLOAD, {"locator": locator, "path": str(path)})
        return ActionResult.model_validate(payload)

    async def click(
        self,
        locator: str,
        *,
        label: str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        payload = await self._ok(
            HostCommand.CLICK,
            {"locator": locator, "label": label},
            idempotency_key=idempotency_key,
        )
        return ActionResult.model_validate(payload)

    async def wait_for_navigation(self, *, timeout_seconds: float | None = None) -> ActionResult:
        payload = await self._ok(
            HostCommand.WAIT_FOR_NAVIGATION,
            {"timeout_seconds": timeout_seconds},
        )
        return ActionResult.model_validate(payload)

    async def screenshot(self, *, relative_path: str) -> str:
        payload = await self._ok(HostCommand.SCREENSHOT, {"relative_path": relative_path})
        return str(payload.get("path", relative_path))

    async def checkpoint(self) -> BrowserCheckpoint:
        payload = await self._ok(HostCommand.CHECKPOINT, {})
        return BrowserCheckpoint.model_validate(payload)

    async def request_human_control(self, instruction: str) -> ActionResult:
        payload = await self._ok(HostCommand.REQUEST_HANDOFF, {"instruction": instruction})
        self._owner = ControlOwner.DELEGATED_TO_USER
        return ActionResult.model_validate(payload)

    async def control_state(self) -> ControlOwner:
        payload = await self._ok(HostCommand.CONTROL_STATE, {})
        self._owner = ControlOwner(str(payload.get("owner", ControlOwner.AGENT.value)))
        return self._owner

    async def wait_for_control(self, *, timeout_seconds: float) -> ActionResult:
        """Poll ownership until the user hands the session back or time runs out.

        A timeout is a report, never a licence: ``ok=False`` means the person is
        still working, and the caller must leave the attempt waiting.
        """
        deadline = monotonic() + timeout_seconds
        while True:
            owner = await self.control_state()
            if owner is ControlOwner.AGENT:
                return ActionResult(ok=True, action="wait")
            remaining = deadline - monotonic()
            if remaining <= 0:
                return ActionResult(
                    ok=False,
                    action="wait",
                    detail=f"user still holds the session after {timeout_seconds:.0f}s",
                )
            await asyncio.sleep(min(_CONTROL_POLL_SECONDS, remaining))

    async def reclaim_control(self, *, confirmed_by_user: bool) -> ActionResult:
        payload = await self._ok(
            HostCommand.RECLAIM_CONTROL,
            {"confirmed_by_user": confirmed_by_user},
        )
        result = ActionResult.model_validate(payload)
        if result.ok:
            self._owner = ControlOwner.AGENT
        return result

    async def close(self) -> None:
        await self._ok(HostCommand.CLOSE_SESSION, {})

    async def _ok(
        self,
        command: HostCommand,
        params: dict[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        try:
            result = await self._manager.dispatch(
                self._host_id,
                command,
                session_id=self.session_id,
                params=params,
                idempotency_key=idempotency_key,
            )
        except HostCommandError as exc:
            return {"ok": False, "action": command.value, "detail": exc.message}
        if not result.ok:
            return {
                "ok": False,
                "action": command.value,
                "detail": result.error_message or result.error_code,
            }
        payload = result.result
        return payload if isinstance(payload, dict) else {}
