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
from applyuminati.browser.host_protocol import HostCommand, HostErrorCode

__all__ = ["HostedBrowserSession"]

#: Gap between ownership polls while waiting for a human to hand control back.
#: The host pushes a control_changed event too, but a poll is what a caller
#: holding a deadline can rely on.
_CONTROL_POLL_SECONDS = 2.0


class HostedBrowserSession:
    """One remote browsing context addressed through :class:`BrowserHostManager`."""

    def __init__(
        self,
        manager: BrowserHostManager,
        host_id: str,
        session_id: str,
        *,
        task_space_id: str | None = None,
    ) -> None:
        self._manager = manager
        self._host_id = host_id
        self.session_id = session_id
        self._task_space_id = task_space_id
        self._owner = ControlOwner.AGENT

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    @property
    def task_space_id(self) -> str | None:
        """What the host reported at creation; the host is the authority."""
        return self._task_space_id

    async def navigate(self, url: str, *, wait_for_load: bool = True) -> PageObservation:
        payload = await self._send(
            HostCommand.NAVIGATE,
            {"url": url, "wait_for_load": wait_for_load},
        )
        return PageObservation.model_validate(payload)

    async def observe(self, *, include_text: bool = True) -> PageObservation:
        payload = await self._send(HostCommand.OBSERVE, {"include_text": include_text})
        return PageObservation.model_validate(payload)

    async def find_controls(self, *, role: ElementRole | None = None) -> list[PageElement]:
        observation = await self.observe()
        if role is None:
            return list(observation.elements)
        return [element for element in observation.elements if element.role is role]

    async def fill_field(self, locator: str, value: str) -> ActionResult:
        return await self._action(HostCommand.FILL, {"locator": locator, "value": value})

    async def select_option(self, locator: str, option: str) -> ActionResult:
        return await self._action(HostCommand.SELECT, {"locator": locator, "option": option})

    async def set_checked(self, locator: str, checked: bool) -> ActionResult:
        return await self._action(HostCommand.SET_CHECKED, {"locator": locator, "checked": checked})

    async def upload_file(self, locator: str, path: Path) -> ActionResult:
        return await self._action(HostCommand.UPLOAD, {"locator": locator, "path": str(path)})

    async def click(
        self,
        locator: str,
        *,
        label: str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        return await self._action(
            HostCommand.CLICK,
            {"locator": locator, "label": label},
            idempotency_key=idempotency_key,
        )

    async def wait_for_navigation(self, *, timeout_seconds: float | None = None) -> ActionResult:
        return await self._action(
            HostCommand.WAIT_FOR_NAVIGATION,
            {"timeout_seconds": timeout_seconds},
        )

    async def screenshot(self, *, relative_path: str) -> str:
        payload = await self._send(HostCommand.SCREENSHOT, {"relative_path": relative_path})
        return str(payload.get("path", relative_path))

    async def checkpoint(self) -> BrowserCheckpoint:
        payload = await self._send(HostCommand.CHECKPOINT, {})
        return BrowserCheckpoint.model_validate(payload)

    async def request_human_control(self, instruction: str) -> ActionResult:
        result = await self._action(HostCommand.REQUEST_HANDOFF, {"instruction": instruction})
        if result.ok:
            self._owner = ControlOwner.DELEGATED_TO_USER
        return result

    async def control_state(self) -> ControlOwner:
        """Ask the host who owns the session. Fails closed.

        An unanswered ownership question is not permission to drive, so a failed
        or malformed reply reports ``USER`` rather than defaulting to ``AGENT``.
        """
        try:
            payload = await self._send(HostCommand.CONTROL_STATE, {})
        except HostCommandError:
            self._owner = ControlOwner.USER
            return self._owner
        try:
            self._owner = ControlOwner(str(payload.get("owner")))
        except ValueError:
            self._owner = ControlOwner.USER
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
        result = await self._action(
            HostCommand.RECLAIM_CONTROL,
            {"confirmed_by_user": confirmed_by_user},
        )
        if result.ok:
            self._owner = ControlOwner.AGENT
        return result

    async def close(self) -> None:
        await self._action(HostCommand.CLOSE_SESSION, {})

    async def _send(
        self,
        command: HostCommand,
        params: dict[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """Dispatch one command, raising when the host did not carry it out.

        Callers needing a structured reply (an observation, a checkpoint) cannot
        be handed a stub: validating a placeholder would surface a host refusal
        as an opaque schema error and lose the reason the host gave.
        """
        result = await self._manager.dispatch(
            self._host_id,
            command,
            session_id=self.session_id,
            params=params,
            idempotency_key=idempotency_key,
        )
        if not result.ok:
            raise HostCommandError(
                result.error_message or "the browser host refused this command",
                code=result.error_code or HostErrorCode.INTERNAL,
                host_id=self._host_id,
                command=command,
            )
        payload = result.result
        return payload if isinstance(payload, dict) else {}

    async def _action(
        self,
        command: HostCommand,
        params: dict[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        """Dispatch one command whose failure is an answer rather than an error.

        A login wall or a user-held session is a result the workflow reads, so
        these come back as ``ok=False`` with the host's detail attached.
        """
        try:
            payload = await self._send(command, params, idempotency_key=idempotency_key)
        except HostCommandError as exc:
            return ActionResult(ok=False, action=command.value, detail=exc.message)
        # The host reports its own ok and action; these only fill a bare reply.
        return ActionResult.model_validate({"ok": True, "action": command.value, **payload})
