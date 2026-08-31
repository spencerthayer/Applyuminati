"""The registry of live Browser Host connections, and command dispatch.

Transport-agnostic on purpose. This module knows about frames and futures, not
about WebSockets: the FastAPI endpoint in
:mod:`applyuminati.api.routers.browser_hosts` adapts one to the other. That
boundary is what lets the protocol be tested against an in-memory connection
rather than a running server, and it is why nothing here imports a web
framework.

The dispatch model is request/response over a stream that may vanish at any
moment, so three failure modes are handled explicitly rather than hoped away:

* **The host goes away mid-command.** Every pending future is failed when the
  connection drops. A caller waiting on a click does not hang until its timeout;
  it learns immediately that the browser is gone.
* **The host answers late.** Results for unknown command ids are dropped. A reply
  arriving after its timeout is not an error and not a surprise, it is just no
  longer wanted.
* **The same host connects twice.** The older connection is closed. Two live
  connections claiming one host id means two things believe they own the same
  browser, and the newer one is the one that just proved it holds the credential.

Sequence numbers are checked for replay. Over a single TCP stream this is
belt-and-braces, but the protocol is meant to survive a proxy, and a frame that
arrives twice must not be executed twice on a page that has moved on.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from applyuminati.browser.host_protocol import (
    CONSEQUENTIAL_COMMANDS,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    CommandMessage,
    ErrorMessage,
    EventMessage,
    HeartbeatMessage,
    HostCommand,
    HostErrorCode,
    RegisteredMessage,
    RegisterMessage,
    ResultMessage,
)
from applyuminati.core.clock import utcnow
from applyuminati.core.errors import ApplyuminatiError, FailureCategory, RecoveryHint
from applyuminati.core.logging import get_logger
from applyuminati.core.models.browser_host import (
    BrowserHostBackend,
    BrowserHostRecord,
    HostConnectionState,
)

log = get_logger(__name__)

__all__ = [
    "BrowserHostManager",
    "HostCommandError",
    "HostConnection",
    "HostSendError",
    "LiveHost",
]


class HostSendError(RuntimeError):
    """The transport could not deliver a frame."""


class HostCommandError(ApplyuminatiError):
    """A command was refused or could not be delivered.

    An :class:`ApplyuminatiError` so a driver's recovery policy can treat a
    vanished Browser Host the same way it treats any other unavailable backend,
    rather than needing a special case for "the user's laptop closed".
    """

    def __init__(
        self,
        message: str,
        *,
        code: HostErrorCode,
        host_id: str | None = None,
        command: HostCommand | None = None,
    ) -> None:
        super().__init__(
            message,
            code=f"browser_host.{code.value}",
            category=_ERROR_CATEGORIES.get(code, FailureCategory.BACKEND_UNAVAILABLE),
            # Left to the category default unless a code needs otherwise, so
            # there is one place recovery policy lives rather than two.
            recovery=_ERROR_RECOVERY.get(code),
            details={"host_id": host_id, "command": command.value if command else None},
        )
        self.error_code = code


_ERROR_CATEGORIES: dict[HostErrorCode, FailureCategory] = {
    HostErrorCode.USER_HAS_CONTROL: FailureCategory.NEEDS_HUMAN,
    HostErrorCode.CAPABILITY_UNAVAILABLE: FailureCategory.BACKEND_UNAVAILABLE,
    HostErrorCode.BACKEND_UNAVAILABLE: FailureCategory.BACKEND_UNAVAILABLE,
    HostErrorCode.TIMED_OUT: FailureCategory.TRANSIENT_NETWORK,
    HostErrorCode.EXPIRED: FailureCategory.TRANSIENT_NETWORK,
    HostErrorCode.UNKNOWN_SESSION: FailureCategory.RESOURCE_GONE,
    HostErrorCode.REVOKED: FailureCategory.AUTH_REQUIRED,
    HostErrorCode.UNAUTHENTICATED: FailureCategory.AUTH_REQUIRED,
    HostErrorCode.PROTOCOL_INCOMPATIBLE: FailureCategory.CONFIGURATION,
}

_ERROR_RECOVERY: dict[HostErrorCode, RecoveryHint] = {
    HostErrorCode.USER_HAS_CONTROL: RecoveryHint.ESCALATE_TO_USER,
    HostErrorCode.REVOKED: RecoveryHint.ESCALATE_TO_USER,
    HostErrorCode.UNAUTHENTICATED: RecoveryHint.ESCALATE_TO_USER,
    HostErrorCode.PROTOCOL_INCOMPATIBLE: RecoveryHint.ESCALATE_TO_USER,
}


class HostConnection(Protocol):
    """One duplex frame channel to a host.

    Deliberately two methods. Anything larger would drag transport concerns into
    this module and make the manager untestable without a server.
    """

    async def send_text(self, payload: str) -> None: ...

    async def close(self, *, code: int = 1000, reason: str = "") -> None: ...


@dataclass(slots=True)
class LiveHost:
    """A connected host: its record, its channel, and its in-flight commands."""

    record: BrowserHostRecord
    connection: HostConnection
    pending: dict[str, asyncio.Future[ResultMessage]] = field(default_factory=dict)
    outbound_seq: int = 0
    #: Highest inbound ``seq`` accepted, for replay rejection.
    inbound_seq: int = -1
    open_sessions: set[str] = field(default_factory=set)

    def next_seq(self) -> int:
        self.outbound_seq += 1
        return self.outbound_seq

    def fail_pending(self, error: BaseException) -> None:
        """Fail every waiting caller. Called exactly once, on disconnect."""
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.pending.clear()


#: Called when a host reports something unsolicited. The workflow layer
#: subscribes so a user taking the browser becomes an intervention rather than a
#: log line nobody reads.
EventHandler = Callable[[BrowserHostRecord, EventMessage], Awaitable[None]]


class BrowserHostManager:
    """Live connections, keyed by host id.

    In-process and not persisted, correctly: a connection cannot outlive the
    process holding it, so writing "connected" to a database would produce a row
    that lies after every restart. Persistence records *pairing*; this records
    *presence*.
    """

    def __init__(self, *, command_timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> None:
        self._hosts: dict[str, LiveHost] = {}
        self._command_timeout = command_timeout
        self._event_handlers: list[EventHandler] = []
        self._lock = asyncio.Lock()

    # -- subscriptions ----------------------------------------------------

    def on_event(self, handler: EventHandler) -> None:
        self._event_handlers.append(handler)

    # -- connection lifecycle ---------------------------------------------

    async def attach(
        self,
        record: BrowserHostRecord,
        connection: HostConnection,
        registration: RegisterMessage,
    ) -> LiveHost:
        """Register a connection, displacing any older one for the same host."""
        async with self._lock:
            existing = self._hosts.get(record.host_id)
            live = LiveHost(record=record, connection=connection)
            live.open_sessions.update(registration.resumable_sessions)
            live.inbound_seq = registration.seq
            self._hosts[record.host_id] = live

        if existing is not None:
            # Two connections claiming one host id means two processes believe
            # they own the same browser. The newer one just proved it holds the
            # credential, so the older one goes.
            log.info("browser_host.displaced", host_id=record.host_id)
            existing.fail_pending(
                HostCommandError(
                    "the host reconnected; this command was in flight and is abandoned",
                    code=HostErrorCode.CANCELLED,
                    host_id=record.host_id,
                )
            )
            with contextlib.suppress(Exception):
                await existing.connection.close(code=1012, reason="replaced by a new connection")

        record.state = HostConnectionState.CONNECTED
        record.last_connected_at = utcnow()
        record.last_seen_at = record.last_connected_at
        record.protocol_version = registration.protocol_version
        record.platform = registration.platform or record.platform
        record.architecture = registration.architecture or record.architecture
        record.host_version = registration.host_version or record.host_version
        record.display_name = registration.display_name or record.display_name
        record.backends = [
            BrowserHostBackend(
                slug=slug,
                available=entry.available,
                preferred=entry.preferred,
                version=entry.version,
                capabilities=list(entry.capabilities),
                detail=entry.detail,
            )
            for slug, entry in sorted(registration.backends.items())
        ]
        record.active_sessions = sorted(live.open_sessions)
        record.last_error = None
        log.info(
            "browser_host.connected",
            host_id=record.host_id,
            platform=record.platform,
            protocol_version=record.protocol_version,
            backends=[b.slug for b in record.backends if b.available],
            resumable_sessions=len(live.open_sessions),
        )
        return live

    def acceptance(self, live: LiveHost) -> RegisteredMessage:
        return RegisteredMessage(
            seq=live.next_seq(),
            host_record_id=live.record.id,
            expected_sessions=sorted(live.open_sessions),
        )

    async def detach(self, host_id: str, *, reason: str = "disconnected") -> None:
        """Drop a connection and fail everything waiting on it."""
        async with self._lock:
            live = self._hosts.pop(host_id, None)
        if live is None:
            return
        live.fail_pending(
            HostCommandError(
                f"the browser host disconnected: {reason}",
                code=HostErrorCode.BACKEND_UNAVAILABLE,
                host_id=host_id,
            )
        )
        live.record.state = HostConnectionState.DISCONNECTED
        live.record.last_error = reason
        log.info("browser_host.disconnected", host_id=host_id, reason=reason)

    def connected(self, host_id: str) -> LiveHost | None:
        return self._hosts.get(host_id)

    def connected_hosts(self) -> list[BrowserHostRecord]:
        return [live.record for live in self._hosts.values()]

    def is_connected(self, host_id: str) -> bool:
        return host_id in self._hosts

    # -- inbound ----------------------------------------------------------

    def check_sequence(self, live: LiveHost, seq: int) -> bool:
        """Accept a frame's sequence number, or reject it as a replay.

        Strictly increasing. A repeated or older ``seq`` is refused rather than
        executed, because the whole risk of a replayed frame is that it acts on a
        page which has since changed.
        """
        if seq <= live.inbound_seq:
            return False
        live.inbound_seq = seq
        return True

    async def handle_result(self, live: LiveHost, message: ResultMessage) -> None:
        """Complete the waiting caller, or drop an unmatched reply."""
        future = live.pending.pop(message.command_id, None)
        if future is None:
            # A reply to a command that already timed out or was abandoned.
            # Expected, and nothing to do about it.
            log.debug(
                "browser_host.unmatched_result",
                host_id=live.record.host_id,
                command_id=message.command_id,
            )
            return
        if not future.done():
            future.set_result(message)

    async def handle_heartbeat(self, live: LiveHost, message: HeartbeatMessage) -> None:
        live.record.last_seen_at = utcnow()
        live.open_sessions = set(message.open_sessions)
        live.record.active_sessions = sorted(live.open_sessions)

    async def handle_event(self, live: LiveHost, message: EventMessage) -> None:
        live.record.last_seen_at = utcnow()
        log.info(
            "browser_host.event",
            host_id=live.record.host_id,
            host_event=message.event.value,
            session=message.session_id,
        )
        for handler in self._event_handlers:
            try:
                await handler(live.record, message)
            except Exception:
                # One bad subscriber must not cost us the connection, and the
                # host is not the party at fault.
                log.warning(
                    "browser_host.event_handler_failed",
                    host_id=live.record.host_id,
                    host_event=message.event.value,
                    exc_info=True,
                )

    async def handle_error(self, live: LiveHost, message: ErrorMessage) -> None:
        live.record.last_error = f"{message.code.value}: {message.message}"[:500]
        log.warning(
            "browser_host.reported_error",
            host_id=live.record.host_id,
            code=message.code.value,
        )

    # -- outbound ---------------------------------------------------------

    async def dispatch(
        self,
        host_id: str,
        command: HostCommand,
        *,
        session_id: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> ResultMessage:
        """Send one command and wait for its result.

        Raises :class:`HostCommandError` when the host is absent, the send fails
        or the deadline passes. A refusal *by* the host comes back as a result
        with ``ok=False``, because a login wall is an answer rather than an
        exception.
        """
        live = self._hosts.get(host_id)
        if live is None:
            raise HostCommandError(
                f"browser host {host_id!r} is not connected",
                code=HostErrorCode.BACKEND_UNAVAILABLE,
                host_id=host_id,
                command=command,
            )
        if command in CONSEQUENTIAL_COMMANDS and not idempotency_key:
            # Refused rather than defaulted. A generated key would be unique per
            # call, which is precisely the deduplication this command needs and
            # would not get, and the failure would only show up as a duplicate
            # application.
            raise HostCommandError(
                f"{command.value} is consequential and requires an idempotency key",
                code=HostErrorCode.MALFORMED,
                host_id=host_id,
                command=command,
            )

        deadline = timeout if timeout is not None else self._command_timeout
        message = CommandMessage(
            seq=live.next_seq(),
            command=command,
            session_id=session_id,
            params=params or {},
            # The host's own deadline, slightly beyond ours, so a command we have
            # already given up on is refused there rather than executed late.
            expires_at=utcnow().timestamp() + deadline + 5.0,
            idempotency_key=idempotency_key,
        )
        future: asyncio.Future[ResultMessage] = asyncio.get_running_loop().create_future()
        live.pending[message.id] = future

        try:
            await live.connection.send_text(message.model_dump_json())
        except Exception as exc:
            live.pending.pop(message.id, None)
            raise HostCommandError(
                f"could not send {command.value} to {host_id!r}: {exc}",
                code=HostErrorCode.BACKEND_UNAVAILABLE,
                host_id=host_id,
                command=command,
            ) from exc

        try:
            return await asyncio.wait_for(future, timeout=deadline)
        except TimeoutError as exc:
            live.pending.pop(message.id, None)
            raise HostCommandError(
                f"{command.value} on {host_id!r} did not answer within {deadline:.0f}s",
                code=HostErrorCode.TIMED_OUT,
                host_id=host_id,
                command=command,
            ) from exc
        except asyncio.CancelledError:
            live.pending.pop(message.id, None)
            raise
        finally:
            live.pending.pop(message.id, None)

    async def send_registered(self, live: LiveHost, message: RegisteredMessage) -> None:
        await live.connection.send_text(message.model_dump_json())

    async def send_error(
        self, connection: HostConnection, code: HostErrorCode, message: str
    ) -> None:
        """Report a refusal on a connection that may not be registered yet."""
        with contextlib.suppress(Exception):
            await connection.send_text(
                ErrorMessage(code=code, message=message[:500]).model_dump_json()
            )

    def mark_stale(self) -> list[BrowserHostRecord]:
        """Flag connections that have stopped heartbeating.

        Stale rather than disconnected: the socket is still open, and the attempt
        the host was running is still resumable when it wakes up. Dropping it to
        disconnected would abandon work that has not actually been lost.
        """
        stale: list[BrowserHostRecord] = []
        now = utcnow()
        for live in self._hosts.values():
            if live.record.is_stale(now=now):
                live.record.state = HostConnectionState.STALE
                stale.append(live.record)
        return stale
