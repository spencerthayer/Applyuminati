"""Map protocol commands onto a local ``BrowserSession``.

Unknown commands and host-scoped execution are refused. ``evaluate`` is
page-scoped and only runs when the selected backend advertised
``javascript_eval``. File upload paths must resolve under the configured
documents directory.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from applyuminati.browser.base import BrowserCapability, BrowserSession, ControlOwner
from applyuminati.browser.host_protocol import (
    CONSEQUENTIAL_COMMANDS,
    CommandMessage,
    HostCommand,
    HostErrorCode,
    ResultMessage,
)
from applyuminati.core.clock import utcnow

__all__ = [
    "HOST_UNDISPATCHABLE_CAPABILITIES",
    "CommandDispatcher",
    "HostSession",
    "host_advertised_capabilities",
]

#: Capabilities a backend may have locally that this host will not execute.
#: Advertising them would let a driver select the host and then fail immediately.
HOST_UNDISPATCHABLE_CAPABILITIES: frozenset[BrowserCapability] = frozenset(
    {
        BrowserCapability.JAVASCRIPT_EVAL,
        BrowserCapability.MULTI_TAB,
        BrowserCapability.DOWNLOADS,
        BrowserCapability.NETWORK_INTERCEPT,
    }
)


def host_advertised_capabilities(capabilities: Iterable[BrowserCapability | str]) -> list[str]:
    """Backend capabilities the dispatcher can actually honour."""
    values: set[str] = set()
    blocked = {item.value for item in HOST_UNDISPATCHABLE_CAPABILITIES}
    for item in capabilities:
        value = item.value if isinstance(item, BrowserCapability) else str(item)
        if value not in blocked:
            values.add(value)
    return sorted(values)


class HostSession:
    """One local browsing context the host is willing to drive."""

    __slots__ = ("backend", "session")

    def __init__(self, session: BrowserSession, backend: str) -> None:
        self.session = session
        self.backend = backend


class CommandDispatcher:
    def __init__(
        self,
        *,
        documents_dir: Path,
        capabilities: frozenset[str],
    ) -> None:
        self.documents_dir = documents_dir.resolve()
        self.capabilities = capabilities
        self._results: dict[str, ResultMessage] = {}

    def refuse_if_stale(self, command: CommandMessage) -> ResultMessage | None:
        if command.expired():
            return ResultMessage(
                command_id=command.id,
                ok=False,
                error_code=HostErrorCode.EXPIRED,
                error_message="command expired before it was executed",
            )
        if command.command in CONSEQUENTIAL_COMMANDS:
            if not command.idempotency_key:
                return ResultMessage(
                    command_id=command.id,
                    ok=False,
                    error_code=HostErrorCode.MALFORMED,
                    error_message="consequential commands require an idempotency key",
                )
            recorded = self._results.get(command.idempotency_key)
            if recorded is not None:
                return recorded.model_copy(update={"command_id": command.id, "deduplicated": True})
        return None

    def remember(self, command: CommandMessage, result: ResultMessage) -> ResultMessage:
        if command.idempotency_key:
            self._results[command.idempotency_key] = result
        return result

    async def execute(self, hosted: HostSession, command: CommandMessage) -> ResultMessage:
        stale = self.refuse_if_stale(command)
        if stale is not None:
            return stale
        started = utcnow()
        try:
            payload = await self._run(hosted, command)
        except _HostRefusal as exc:
            result = ResultMessage(
                command_id=command.id,
                ok=False,
                error_code=exc.code,
                error_message=exc.message[:300],
                duration_ms=(utcnow() - started).total_seconds() * 1000,
            )
            return self.remember(command, result)
        except Exception as exc:
            result = ResultMessage(
                command_id=command.id,
                ok=False,
                error_code=HostErrorCode.INTERNAL,
                error_message=str(exc)[:300],
                duration_ms=(utcnow() - started).total_seconds() * 1000,
            )
            return self.remember(command, result)
        result = ResultMessage(
            command_id=command.id,
            ok=True,
            result=payload,
            duration_ms=(utcnow() - started).total_seconds() * 1000,
        )
        return self.remember(command, result)

    async def _run(self, hosted: HostSession, command: CommandMessage) -> dict[str, Any]:
        session = hosted.session
        params = command.params
        owner = await session.control_state()
        acting = command.command not in {
            HostCommand.CONTROL_STATE,
            HostCommand.RECLAIM_CONTROL,
            HostCommand.OBSERVE,
            HostCommand.SCREENSHOT,
            HostCommand.CHECKPOINT,
            HostCommand.HEALTH,
            HostCommand.CANCEL,
            HostCommand.CLOSE_SESSION,
        }
        if acting and owner is not ControlOwner.AGENT:
            msg = "the user currently owns this session"
            raise _HostRefusal(HostErrorCode.USER_HAS_CONTROL, msg)

        if command.command is HostCommand.NAVIGATE:
            observation = await session.navigate(str(params["url"]))
            return observation.model_dump(mode="json")
        if command.command is HostCommand.OBSERVE:
            observation = await session.observe()
            return observation.model_dump(mode="json")
        if command.command is HostCommand.CLICK:
            result = await session.click(str(params["locator"]), label=params.get("label"))
            return result.model_dump(mode="json")
        if command.command is HostCommand.FILL:
            result = await session.fill_field(str(params["locator"]), str(params["value"]))
            return result.model_dump(mode="json")
        if command.command is HostCommand.SELECT:
            result = await session.select_option(str(params["locator"]), str(params["option"]))
            return result.model_dump(mode="json")
        if command.command is HostCommand.SET_CHECKED:
            result = await session.set_checked(str(params["locator"]), bool(params["checked"]))
            return result.model_dump(mode="json")
        if command.command is HostCommand.UPLOAD:
            path = self._safe_upload_path(str(params["path"]))
            result = await session.upload_file(str(params["locator"]), path)
            return result.model_dump(mode="json")
        if command.command is HostCommand.SCREENSHOT:
            stored = await session.screenshot(
                relative_path=str(params.get("relative_path", "shot.png"))
            )
            return {"path": stored}
        if command.command is HostCommand.WAIT_FOR_NAVIGATION:
            result = await session.wait_for_navigation()
            return result.model_dump(mode="json")
        if command.command is HostCommand.CHECKPOINT:
            checkpoint = await session.checkpoint()
            return checkpoint.model_dump(mode="json")
        if command.command is HostCommand.REQUEST_HANDOFF:
            result = await session.request_human_control(str(params.get("instruction", "")))
            return result.model_dump(mode="json")
        if command.command is HostCommand.RECLAIM_CONTROL:
            result = await session.reclaim_control(
                confirmed_by_user=bool(params.get("confirmed_by_user"))
            )
            return result.model_dump(mode="json")
        if command.command is HostCommand.CONTROL_STATE:
            return {"owner": (await session.control_state()).value}
        if command.command is HostCommand.EVALUATE:
            if BrowserCapability.JAVASCRIPT_EVAL.value not in self.capabilities:
                msg = "javascript_eval is not advertised by this backend"
                raise _HostRefusal(HostErrorCode.CAPABILITY_UNAVAILABLE, msg)
            msg = "evaluate is not exposed as host-scoped script"
            raise _HostRefusal(HostErrorCode.UNKNOWN_COMMAND, msg)
        if command.command in {
            HostCommand.OPEN_TAB,
            HostCommand.CLOSE_TAB,
            HostCommand.ACTIVATE_TAB,
            HostCommand.LIST_TABS,
            HostCommand.DOWNLOAD,
        }:
            msg = f"{command.command.value} is not implemented on this host yet"
            raise _HostRefusal(HostErrorCode.CAPABILITY_UNAVAILABLE, msg)
        if command.command is HostCommand.HEALTH:
            return {"backend": hosted.backend}
        if command.command is HostCommand.CANCEL:
            return {"cancelled": True}
        if command.command is HostCommand.CLOSE_SESSION:
            await session.close()
            return {"closed": True}
        if command.command is HostCommand.CREATE_SESSION:
            return {
                "session_id": session.session_id,
                "backend": hosted.backend,
                "task_space_id": session.task_space_id,
            }
        msg = f"unknown command {command.command.value}"
        raise _HostRefusal(HostErrorCode.UNKNOWN_COMMAND, msg)

    def _safe_upload_path(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.documents_dir / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.documents_dir):
            msg = "upload path is outside the host documents directory"
            raise _HostRefusal(HostErrorCode.MALFORMED, msg)
        return resolved


class _HostRefusal(Exception):
    def __init__(self, code: HostErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
