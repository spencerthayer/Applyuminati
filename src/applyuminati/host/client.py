"""Outbound WebSocket client for applyuminati-browser-host."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from applyuminati.browser.host_protocol import (
    HEARTBEAT_INTERVAL_SECONDS,
    PROTOCOL_VERSION,
    CommandMessage,
    ErrorMessage,
    HostCommand,
    HostErrorCode,
    MessageType,
    RegisteredMessage,
    RegisterMessage,
    decode_message,
)
from applyuminati.core.logging import get_logger
from applyuminati.host.dispatcher import CommandDispatcher, HostSession
from applyuminati.host.security import require_secure_server

log = get_logger(__name__)

__all__ = ["HostClient", "open_local_session"]


async def open_local_session(settings: Any, *, backend: str | None = None) -> HostSession:
    """Open a local browser session using registered plugins."""
    from applyuminati.browser.base import BROWSER_REGISTRY
    from applyuminati.plugins.browsers import register_browsers

    register_browsers()
    slug = backend or "ego_lite"
    if slug not in BROWSER_REGISTRY:
        slug = next(iter(BROWSER_REGISTRY.slugs()), slug)
    descriptor = BROWSER_REGISTRY.get(slug)
    try:
        impl = descriptor.create(settings=settings)
    except TypeError:
        impl = descriptor.create()
    session = await impl.open_session()
    return HostSession(session, slug)


class HostClient:
    """Connect, register, dispatch, heartbeat, reconnect."""

    def __init__(
        self,
        *,
        server: str,
        host_id: str,
        credential: str,
        dispatcher: CommandDispatcher,
        backends: dict[str, Any],
        allow_insecure: bool = False,
        session_factory: Callable[..., Any] | None = None,
        host_version: str = "0.1.0",
    ) -> None:
        require_secure_server(server, allow_insecure=allow_insecure)
        self.server = server
        self.host_id = host_id
        self._credential = credential
        self.dispatcher = dispatcher
        self.backends = backends
        self.session_factory = session_factory
        self.host_version = host_version
        self._sessions: dict[str, HostSession] = {}
        self._outbound_seq = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._once()
                delay = 1.0
            except Exception:
                log.warning("browser_host.reconnect", host_id=self.host_id, delay=delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, 30.0)

    async def _once(self) -> None:
        import websockets

        parsed = urlparse(self.server)
        async with websockets.connect(self.server, open_timeout=10) as socket:
            await socket.send(self._registration().model_dump_json())
            raw = await socket.recv()
            message = decode_message(raw if isinstance(raw, str | bytes) else str(raw))
            if isinstance(message, ErrorMessage):
                log.warning("browser_host.rejected", code=message.code.value)
                return
            if not isinstance(message, RegisteredMessage):
                log.warning("browser_host.unexpected_hello")
                return
            log.info(
                "browser_host.registered",
                host_id=self.host_id,
                server=parsed.hostname,
                protocol=message.protocol_version,
            )
            await self._loop(socket)

    def _registration(self) -> RegisterMessage:
        self._outbound_seq += 1
        return RegisterMessage(
            seq=self._outbound_seq,
            protocol_version=PROTOCOL_VERSION,
            host_id=self.host_id,
            credential=self._credential,
            platform=sys.platform,
            host_version=self.host_version,
            backends=self.backends,
            resumable_sessions=sorted(self._sessions),
        )

    async def _loop(self, socket: Any) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(socket))
        try:
            async for raw in socket:
                if self._stop.is_set():
                    return
                await self._handle(socket, raw if isinstance(raw, str | bytes) else str(raw))
        finally:
            heartbeat.cancel()

    async def _heartbeat(self, socket: Any) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            self._outbound_seq += 1
            await socket.send(
                json.dumps(
                    {
                        "type": MessageType.HEARTBEAT.value,
                        "seq": self._outbound_seq,
                        "open_sessions": sorted(self._sessions),
                    }
                )
            )

    async def _handle(self, socket: Any, raw: str | bytes) -> None:
        payload = json.loads(raw)
        if payload.get("type") != MessageType.COMMAND.value:
            return
        command = CommandMessage.model_validate(payload)
        if command.command is HostCommand.CREATE_SESSION:
            if self.session_factory is None:
                result = {
                    "type": "result",
                    "command_id": command.id,
                    "ok": False,
                    "error_code": HostErrorCode.BACKEND_UNAVAILABLE.value,
                    "error_message": "no local session factory",
                }
                await socket.send(json.dumps(result))
                return
            hosted = await self.session_factory(backend=command.params.get("backend"))
            self._sessions[hosted.session.session_id] = hosted
            result = {
                "type": "result",
                "command_id": command.id,
                "ok": True,
                "result": {"session_id": hosted.session.session_id, "backend": hosted.backend},
            }
            await socket.send(json.dumps(result))
            return
        hosted = self._sessions.get(command.session_id or "")
        if hosted is None and command.command is not HostCommand.HEALTH:
            await socket.send(
                json.dumps(
                    {
                        "type": "result",
                        "command_id": command.id,
                        "ok": False,
                        "error_code": HostErrorCode.UNKNOWN_SESSION.value,
                        "error_message": "unknown session",
                    }
                )
            )
            return
        if hosted is None:
            await socket.send(
                json.dumps({"type": "result", "command_id": command.id, "ok": True, "result": {}})
            )
            return
        result = await self.dispatcher.execute(hosted, command)
        if command.command is HostCommand.CLOSE_SESSION:
            self._sessions.pop(command.session_id or "", None)
        await socket.send(result.model_dump_json())
