"""Browser Host pairing and the WebSocket the hosts connect to.

Two authentication systems meet in this module and they are deliberately kept
apart:

* The **REST routes** are human operations behind the session cookie: pair a
  host, list hosts, revoke a credential. They inherit
  :class:`~applyuminati.api.security.AuthenticationMiddleware` like everything
  else under ``/api``.
* The **WebSocket** is a machine credential presented in the first frame, and it
  is exempt from the human middleware because a host has no session and no
  browser to hold a cookie.

Exempting the socket is only safe because it authenticates itself before doing
anything: a connection that does not present a valid credential in its first
frame is closed, and until that frame arrives it can send nothing else and
cannot address a session. Keeping the two credential types unable to substitute
for one another is the point. A leaked host token must not read a resume, and a
stolen session cookie must not drive a browser.

The socket handler is thin on purpose. Framing lives in
:mod:`applyuminati.browser.host_protocol` and dispatch in
:mod:`applyuminati.browser.host_manager`, so the protocol is testable without a
running server and this module only translates between a Starlette WebSocket and
those two.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketState

from applyuminati.api.dependencies import get_container_dep
from applyuminati.browser.host_manager import BrowserHostManager, LiveHost
from applyuminati.browser.host_protocol import (
    PROTOCOL_VERSION,
    WEBSOCKET_PATH,
    ErrorMessage,
    EventMessage,
    HeartbeatMessage,
    HostErrorCode,
    ProtocolError,
    RegisterMessage,
    ResultMessage,
    decode_message,
    protocol_compatible,
)
from applyuminati.core.logging import get_logger
from applyuminati.core.models.browser_host import BrowserHostRecord, HostConnectionState
from applyuminati.services.container import ServiceContainer

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/browser-hosts", tags=["browser-hosts"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BrowserHostBackendInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    available: bool
    preferred: bool
    version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    detail: str | None = None


class BrowserHostInfo(BaseModel):
    """A host as the UI sees it. Contains no credential material."""

    model_config = ConfigDict(extra="forbid")

    id: str
    host_id: str
    display_name: str | None = None
    platform: str | None = None
    architecture: str | None = None
    host_version: str | None = None
    protocol_version: int | None = None
    state: HostConnectionState
    backends: list[BrowserHostBackendInfo] = Field(default_factory=list)
    #: Leading characters of the credential only, so two tokens can be told
    #: apart when revoking one.
    credential_prefix: str | None = None
    paired_at: datetime
    last_seen_at: datetime | None = None
    last_connected_at: datetime | None = None
    last_error: str | None = None
    active_sessions: list[str] = Field(default_factory=list)
    #: Reconciled against the live connection registry rather than read from the
    #: stored state column, which is a leftover after any restart.
    connected: bool = False


class PairHostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    display_name: str | None = Field(default=None, max_length=128)


class PairHostResponse(BaseModel):
    """The one and only time the credential is returned."""

    model_config = ConfigDict(extra="forbid")

    host: BrowserHostInfo
    #: Shown once. Not stored, not recoverable, not logged.
    credential: str
    websocket_path: str = WEBSOCKET_PATH
    protocol_version: int = PROTOCOL_VERSION


def _to_info(record: BrowserHostRecord, *, connected: bool) -> BrowserHostInfo:
    return BrowserHostInfo(
        id=record.id,
        host_id=record.host_id,
        display_name=record.display_name,
        platform=record.platform,
        architecture=record.architecture,
        host_version=record.host_version,
        protocol_version=record.protocol_version,
        # The live registry wins: a stored "connected" is a cache, and after a
        # restart it is a cache that lies.
        state=record.state if connected else _offline_state(record),
        backends=[
            BrowserHostBackendInfo(
                slug=entry.slug,
                available=entry.available,
                preferred=entry.preferred,
                version=entry.version,
                capabilities=list(entry.capabilities),
                detail=entry.detail,
            )
            for entry in record.backends
        ],
        credential_prefix=record.credential_prefix,
        paired_at=record.paired_at,
        last_seen_at=record.last_seen_at,
        last_connected_at=record.last_connected_at,
        last_error=record.last_error,
        active_sessions=list(record.active_sessions),
        connected=connected,
    )


def _offline_state(record: BrowserHostRecord) -> HostConnectionState:
    if record.state in (HostConnectionState.REVOKED, HostConnectionState.INCOMPATIBLE):
        return record.state
    if record.state is HostConnectionState.REGISTERED and record.last_connected_at is None:
        return HostConnectionState.REGISTERED
    return HostConnectionState.DISCONNECTED


# ---------------------------------------------------------------------------
# Human-facing routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[BrowserHostInfo])
async def list_hosts(
    container: ServiceContainer = Depends(get_container_dep),
) -> list[BrowserHostInfo]:
    async with container.read_repositories() as repos:
        records = await repos.browser_hosts.list()
    manager = container.browser_hosts
    infos: list[BrowserHostInfo] = []
    for record in records:
        live = manager.connected(record.host_id)
        # The live record has the advertisement from this connection; the stored
        # row is what the last process wrote and is stale while a host is up.
        source = live.record if live is not None else record
        infos.append(_to_info(source, connected=live is not None))
    return infos


@router.post("/pair", response_model=PairHostResponse, status_code=status.HTTP_201_CREATED)
async def pair_host(
    request: PairHostRequest,
    container: ServiceContainer = Depends(get_container_dep),
) -> PairHostResponse:
    """Mint a credential for a host, or rotate an existing one.

    Rotating rather than refusing a duplicate ``host_id`` makes "I lost the
    token" a supported operation, and the previous secret stops working the
    moment this returns.
    """
    async with container.repositories() as repos:
        paired = await repos.browser_hosts.pair(
            host_id=request.host_id, display_name=request.display_name
        )
    # The credential is in the response body and nowhere else: not in this log
    # line, not in the database, not recoverable afterwards.
    log.info("browser_host.paired", host_id=paired.record.host_id)
    return PairHostResponse(
        host=_to_info(paired.record, connected=False),
        credential=paired.secret,
    )


@router.post("/{host_id}/revoke", response_model=BrowserHostInfo)
async def revoke_host(
    host_id: str,
    container: ServiceContainer = Depends(get_container_dep),
) -> BrowserHostInfo:
    """Withdraw a credential and drop the connection using it."""
    async with container.repositories() as repos:
        record = await repos.browser_hosts.revoke(host_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown browser host")
    # Revoking a credential while the host is connected has to close the socket
    # too, or revocation would only take effect at the next reconnect.
    await container.browser_hosts.detach(host_id, reason="credential revoked")
    log.info("browser_host.revoked", host_id=host_id)
    return _to_info(record, connected=False)


# ---------------------------------------------------------------------------
# The host socket
# ---------------------------------------------------------------------------


class _WebSocketConnection:
    """Adapts a Starlette WebSocket to the manager's transport protocol."""

    __slots__ = ("_socket",)

    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket

    async def send_text(self, payload: str) -> None:
        if self._socket.client_state is not WebSocketState.CONNECTED:
            msg = "the browser host socket is closed"
            raise RuntimeError(msg)
        await self._socket.send_text(payload)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        if self._socket.client_state is WebSocketState.CONNECTED:
            await self._socket.close(code=code, reason=reason[:120])


async def _authenticate(
    socket: WebSocket, container: ServiceContainer
) -> tuple[BrowserHostRecord, RegisterMessage] | None:
    """Read and verify the first frame. Returns ``None`` after closing the socket.

    Nothing else happens on this connection until this succeeds, which is what
    makes exempting the path from the human session middleware acceptable.
    """
    try:
        raw = await socket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return None

    try:
        message = decode_message(raw)
    except ProtocolError as exc:
        await _refuse(socket, exc.code, exc.message)
        return None

    if not isinstance(message, RegisterMessage):
        await _refuse(socket, HostErrorCode.MALFORMED, "the first frame must be a register message")
        return None

    if not protocol_compatible(message.protocol_version):
        # Refused rather than tolerated: guessing at framing we do not know,
        # against a process driving someone's authenticated browser, is not worth
        # avoiding an upgrade prompt.
        await _refuse(
            socket,
            HostErrorCode.PROTOCOL_INCOMPATIBLE,
            f"this server speaks protocol {PROTOCOL_VERSION}, the host speaks "
            f"{message.protocol_version}",
        )
        return None

    async with container.repositories() as repos:
        record = await repos.browser_hosts.authenticate(
            host_id=message.host_id, credential=message.credential
        )
    if record is None:
        # One answer for unknown, revoked and wrong-secret. A socket that drives
        # someone's browser must not also be an oracle for which hosts exist.
        log.warning("browser_host.rejected", host_id=message.host_id)
        await _refuse(socket, HostErrorCode.UNAUTHENTICATED, "pairing credential is not valid")
        return None
    return record, message


async def _refuse(socket: WebSocket, code: HostErrorCode, message: str) -> None:
    """Report a code, then close. No stack traces cross this boundary."""
    try:
        await socket.send_text(ErrorMessage(code=code, message=message[:200]).model_dump_json())
        await socket.close(code=1008, reason=code.value)
    except (WebSocketDisconnect, RuntimeError):
        return


async def _persist_snapshot(container: ServiceContainer, record: BrowserHostRecord) -> None:
    """Write the in-memory host record. Never raises into the socket handler."""
    try:
        async with container.repositories() as repos:
            await repos.browser_hosts.save(record)
    except Exception:
        log.warning("browser_host.persist_failed", host_id=record.host_id, exc_info=True)


@router.websocket("/ws")
async def browser_host_socket(
    socket: WebSocket,
    container: ServiceContainer = Depends(get_container_dep),
) -> None:
    """The single inbound endpoint for Browser Hosts.

    Accepted before authentication because a WebSocket cannot carry a body
    otherwise; the first frame must then be a valid registration or the socket is
    closed having done nothing.
    """
    await socket.accept()
    authenticated = await _authenticate(socket, container)
    if authenticated is None:
        return
    record, registration = authenticated

    manager: BrowserHostManager = container.browser_hosts
    connection = _WebSocketConnection(socket)
    live = await manager.attach(record, connection, registration)
    accepted = manager.acceptance(live)
    await connection.send_text(accepted.model_dump_json())

    reason = "closed by the host"
    try:
        await _pump(socket, manager, live)
    except WebSocketDisconnect:
        reason = "socket disconnected"
    except Exception:
        reason = "server error handling a host frame"
        log.warning("browser_host.socket_failed", host_id=record.host_id, exc_info=True)
    finally:
        snapshot = live.record.model_copy(deep=True)
        snapshot.state = HostConnectionState.DISCONNECTED
        snapshot.last_error = reason
        await manager.detach(record.host_id, reason=reason)
        await _persist_snapshot(container, snapshot)


async def _pump(
    socket: WebSocket,
    manager: BrowserHostManager,
    live: LiveHost,
) -> None:
    """Read frames until the socket closes, routing each to the manager.

    Persistence stays out of this loop. Heartbeats and events update the
    in-memory record; the disconnect path writes it once. A write per frame
    would stall the socket on SQLite and is how a TestClient websocket
    deadlocks: the portal is waiting for the next send while this handler is
    waiting for a session.
    """
    while True:
        raw = await socket.receive_text()
        try:
            message = decode_message(raw)
        except ProtocolError as exc:
            # A malformed frame is the host's problem, not a reason to drop a
            # connection that may be holding a half-finished application.
            log.warning("browser_host.bad_frame", host_id=live.record.host_id, code=exc.code.value)
            await manager.send_error(live.connection, exc.code, exc.message)
            continue

        if not manager.check_sequence(live, message.seq):
            await manager.send_error(
                live.connection, HostErrorCode.REPLAYED, f"sequence {message.seq} already seen"
            )
            continue

        if isinstance(message, ResultMessage):
            await manager.handle_result(live, message)
        elif isinstance(message, HeartbeatMessage):
            await manager.handle_heartbeat(live, message)
        elif isinstance(message, EventMessage):
            await manager.handle_event(live, message)
        elif isinstance(message, ErrorMessage):
            await manager.handle_error(live, message)
        elif isinstance(message, RegisterMessage):
            # Re-registering on a live connection is not how reconnection works;
            # a host that wants to change its advertisement opens a new socket.
            await manager.send_error(
                live.connection,
                HostErrorCode.MALFORMED,
                "already registered on this connection",
            )


__all__ = [
    "BrowserHostBackendInfo",
    "BrowserHostInfo",
    "PairHostRequest",
    "PairHostResponse",
    "router",
]
