"""Browser Host protocol, pairing, dispatch and the socket endpoint.

This connection drives a real person's authenticated browser, so the tests that
matter most here are the refusals: an unpaired host, a revoked credential, a
protocol version we do not speak, a replayed frame, a command that outlived its
deadline, and a consequential command with no deduplication key.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr
from websockets.sync.client import connect as ws_connect

from applyuminati.api.app import create_app
from applyuminati.browser.host_manager import (
    BrowserHostManager,
    HostCommandError,
    LiveHost,
)
from applyuminati.browser.host_protocol import (
    MAX_MESSAGE_BYTES,
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    WEBSOCKET_PATH,
    BackendAdvertisement,
    CommandMessage,
    ErrorMessage,
    EventMessage,
    HeartbeatMessage,
    HostCommand,
    HostErrorCode,
    HostEvent,
    ProtocolError,
    RegisteredMessage,
    RegisterMessage,
    ResultMessage,
    decode_message,
    protocol_compatible,
)
from applyuminati.core.models.browser_host import (
    STALE_AFTER,
    BrowserHostBackend,
    BrowserHostRecord,
    HostConnectionState,
)
from applyuminati.core.security import verify_host_credential
from applyuminati.core.settings import SecuritySettings
from applyuminati.db.repositories.browser_hosts import BrowserHostRepository
from applyuminati.db.session import set_database
from applyuminati.services.container import set_container

HOST_ID = "spencers-mac"


class FakeConnection:
    """Captures frames instead of writing them to a socket."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None
        self._fail = fail

    async def send_text(self, payload: str) -> None:
        if self._fail:
            msg = "socket is gone"
            raise RuntimeError(msg)
        self.sent.append(payload)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


def _registration(**overrides) -> RegisterMessage:
    payload = {
        "seq": 1,
        "host_id": HOST_ID,
        "credential": "secret-value",
        "platform": "darwin",
        "architecture": "arm64",
        "host_version": "0.1.0",
        "backends": {
            "ego_lite": BackendAdvertisement(
                available=True,
                preferred=True,
                capabilities=["navigate", "human_handoff", "authenticated_user_profile"],
            ),
            "playwright": BackendAdvertisement(available=True, capabilities=["navigate"]),
        },
    }
    payload.update(overrides)
    return RegisterMessage.model_validate(payload)


def _record(**overrides) -> BrowserHostRecord:
    payload = {"host_id": HOST_ID, "credential_hash": "x" * 64}
    payload.update(overrides)
    return BrowserHostRecord.model_validate(payload)


async def _attach(
    manager: BrowserHostManager, connection: FakeConnection | None = None
) -> tuple[LiveHost, FakeConnection]:
    conn = connection or FakeConnection()
    live = await manager.attach(_record(), conn, _registration())
    return live, conn


# ---------------------------------------------------------------------------
# Protocol framing
# ---------------------------------------------------------------------------


def test_the_command_set_has_no_way_to_run_anything_on_the_host() -> None:
    """The central safety property. A host obeys only browser operations.

    A protocol able to say "run this" would be a remote shell into the most
    sensitive machine in the deployment, reachable by whatever compromises the
    server.
    """
    names = {command.value for command in HostCommand}
    for forbidden in ("exec", "shell", "run_script", "read_file", "write_file", "spawn"):
        assert not any(forbidden in name for name in names)
    # `evaluate` runs inside the page, which is bookmarklet authority, not host
    # authority, and is capability-gated.
    assert "evaluate" in names


def test_a_register_frame_round_trips() -> None:
    message = decode_message(_registration().model_dump_json())
    assert isinstance(message, RegisterMessage)
    assert message.host_id == HOST_ID
    assert message.backends["ego_lite"].preferred


def test_the_credential_is_not_in_a_repr() -> None:
    """A frame logged at debug level must not leak the pairing secret."""
    assert "secret-value" not in repr(_registration())


def test_unknown_message_types_are_refused() -> None:
    with pytest.raises(ProtocolError) as raised:
        decode_message('{"type": "please_run_this", "seq": 1}')
    assert raised.value.code is HostErrorCode.MALFORMED


def test_unknown_fields_are_refused_rather_than_ignored() -> None:
    """Interpreting a frame we do not fully understand acts in someone's browser."""
    with pytest.raises(ProtocolError):
        decode_message('{"type": "heartbeat", "seq": 1, "surprise": true}')


def test_non_json_and_non_object_frames_are_refused() -> None:
    for raw in ("not json", "[1, 2, 3]", '"a string"', "null"):
        with pytest.raises(ProtocolError) as raised:
            decode_message(raw)
        assert raised.value.code is HostErrorCode.MALFORMED


def test_oversized_frames_are_refused_before_parsing() -> None:
    oversized = '{"type": "heartbeat", "seq": 1, "pad": "' + "a" * MAX_MESSAGE_BYTES + '"}'
    with pytest.raises(ProtocolError) as raised:
        decode_message(oversized)
    assert raised.value.code is HostErrorCode.MESSAGE_TOO_LARGE


def test_a_server_to_host_frame_is_not_accepted_as_inbound() -> None:
    """Only the host's half of the protocol is parsed from the wire."""
    with pytest.raises(ProtocolError):
        decode_message(RegisteredMessage(host_record_id="x").model_dump_json())
    with pytest.raises(ProtocolError):
        decode_message(CommandMessage(command=HostCommand.OBSERVE).model_dump_json())


@pytest.mark.parametrize(
    ("version", "compatible"),
    [
        (PROTOCOL_VERSION, True),
        (MIN_PROTOCOL_VERSION, True),
        (PROTOCOL_VERSION + 1, False),
        (MIN_PROTOCOL_VERSION - 1, False),
        (None, False),
    ],
)
def test_protocol_compatibility_is_a_closed_range(version, compatible) -> None:
    """A newer host is refused, not tolerated. Guessing at framing is not free."""
    assert protocol_compatible(version) is compatible


def test_a_command_knows_when_it_has_expired() -> None:
    command = CommandMessage(command=HostCommand.CLICK, expires_at=1000.0)
    assert not command.expired(now=999.0)
    assert command.expired(now=1000.0)
    assert not CommandMessage(command=HostCommand.OBSERVE).expired(now=1e12)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def test_capabilities_are_the_union_across_available_backends() -> None:
    """The host runs whichever backend the server picks, so union is correct."""
    record = _record(
        backends=[
            BrowserHostBackend(slug="ego_lite", available=True, capabilities=["human_handoff"]),
            BrowserHostBackend(slug="playwright", available=True, capabilities=["headless"]),
        ]
    )
    assert record.capabilities() == frozenset({"human_handoff", "headless"})
    assert record.capabilities(backend="playwright") == frozenset({"headless"})


def test_an_unavailable_backend_contributes_nothing() -> None:
    record = _record(
        backends=[
            BrowserHostBackend(slug="ego_lite", available=False, capabilities=["human_handoff"])
        ]
    )
    assert record.capabilities() == frozenset()
    assert not record.usable()


def test_a_host_is_only_usable_when_paired_connected_and_capable() -> None:
    backends = [BrowserHostBackend(slug="ego_lite", available=True)]
    assert _record(state=HostConnectionState.CONNECTED, backends=backends).usable()
    assert not _record(state=HostConnectionState.DISCONNECTED, backends=backends).usable()
    assert not _record(
        state=HostConnectionState.CONNECTED, backends=backends, credential_hash=None
    ).usable()


def test_a_revoked_host_is_not_paired() -> None:
    from applyuminati.core.clock import utcnow

    record = _record(revoked_at=utcnow())
    assert record.revoked
    assert not record.paired


def test_staleness_only_applies_to_a_connected_host() -> None:
    from applyuminati.core.clock import utcnow

    now = utcnow()
    assert _record(
        state=HostConnectionState.CONNECTED, last_seen_at=now - STALE_AFTER * 2
    ).is_stale(now=now)
    assert not _record(state=HostConnectionState.CONNECTED, last_seen_at=now).is_stale(now=now)
    assert not _record(state=HostConnectionState.DISCONNECTED).is_stale(now=now)


def test_the_host_record_carries_no_machine_fingerprint() -> None:
    """This record is shown in the UI and in logs. It is not an inventory."""
    fields = set(BrowserHostRecord.model_fields)
    for forbidden in ("serial", "username", "user", "mac_address", "hostname_fqdn", "installed"):
        assert forbidden not in fields


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


async def test_attaching_records_what_the_host_advertised() -> None:
    manager = BrowserHostManager()
    live, _ = await _attach(manager)
    assert live.record.state is HostConnectionState.CONNECTED
    assert live.record.platform == "darwin"
    assert [b.slug for b in live.record.backends] == ["ego_lite", "playwright"]
    assert manager.is_connected(HOST_ID)


async def test_an_unknown_capability_string_does_not_break_registration() -> None:
    """A host on a newer build must not make every server upgrade a flag day."""
    manager = BrowserHostManager()
    registration = _registration(
        backends={"ego_lite": BackendAdvertisement(available=True, capabilities=["time_travel"])}
    )
    live = await manager.attach(_record(), FakeConnection(), registration)
    assert "time_travel" in live.record.capabilities()


async def test_a_second_connection_displaces_the_first() -> None:
    """Two live connections for one host id means two things own one browser."""
    manager = BrowserHostManager()
    _, first = await _attach(manager)
    _, second = await _attach(manager)
    assert first.closed is not None
    assert second.closed is None
    assert manager.is_connected(HOST_ID)


async def test_displacing_fails_the_old_connections_in_flight_commands() -> None:
    manager = BrowserHostManager()
    live, _ = await _attach(manager)
    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    live.pending["cmd"] = pending
    await _attach(manager)
    with pytest.raises(HostCommandError):
        await pending


async def test_disconnecting_fails_every_waiting_caller_immediately() -> None:
    """A caller waiting on a click should not hang until its timeout."""
    manager = BrowserHostManager()
    live, _ = await _attach(manager)
    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    live.pending["cmd"] = pending
    await manager.detach(HOST_ID, reason="laptop closed")
    assert not manager.is_connected(HOST_ID)
    with pytest.raises(HostCommandError) as raised:
        await pending
    assert "laptop closed" in str(raised.value)
    assert live.record.state is HostConnectionState.DISCONNECTED


async def test_detaching_an_unknown_host_is_harmless() -> None:
    await BrowserHostManager().detach("never-seen")


async def test_sequence_numbers_must_increase() -> None:
    manager = BrowserHostManager()
    live, _ = await _attach(manager)
    assert manager.check_sequence(live, 2)
    assert manager.check_sequence(live, 3)
    # A replayed frame must not be executed against a page that has moved on.
    assert not manager.check_sequence(live, 3)
    assert not manager.check_sequence(live, 1)


async def test_a_heartbeat_reconciles_the_open_session_list() -> None:
    manager = BrowserHostManager()
    live, _ = await _attach(manager)
    await manager.handle_heartbeat(live, HeartbeatMessage(seq=2, open_sessions=["s1", "s2"]))
    assert live.record.active_sessions == ["s1", "s2"]
    assert live.record.last_seen_at is not None


async def test_events_reach_subscribers() -> None:
    manager = BrowserHostManager()
    seen: list[HostEvent] = []

    async def handler(record, message) -> None:
        seen.append(message.event)

    manager.on_event(handler)
    live, _ = await _attach(manager)
    await manager.handle_event(live, EventMessage(seq=2, event=HostEvent.CONTROL_CHANGED))
    assert seen == [HostEvent.CONTROL_CHANGED]


async def test_a_failing_subscriber_does_not_cost_the_connection() -> None:
    manager = BrowserHostManager()

    async def broken(record, message) -> None:
        msg = "subscriber is broken"
        raise RuntimeError(msg)

    manager.on_event(broken)
    live, _ = await _attach(manager)
    await manager.handle_event(live, EventMessage(seq=2, event=HostEvent.NAVIGATION))
    assert manager.is_connected(HOST_ID)


async def test_marking_stale_does_not_disconnect() -> None:
    """A laptop that slept has not been decommissioned; its attempt is resumable."""
    from applyuminati.core.clock import utcnow

    manager = BrowserHostManager()
    live, _ = await _attach(manager)
    live.record.last_seen_at = utcnow() - STALE_AFTER * 2
    assert [r.host_id for r in manager.mark_stale()] == [HOST_ID]
    assert live.record.state is HostConnectionState.STALE
    assert manager.is_connected(HOST_ID)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def test_a_command_resolves_with_the_matching_result() -> None:
    manager = BrowserHostManager()
    live, connection = await _attach(manager)

    async def answer() -> None:
        await asyncio.sleep(0)
        sent = CommandMessage.model_validate_json(connection.sent[-1])
        await manager.handle_result(
            live, ResultMessage(seq=2, command_id=sent.id, ok=True, result={"url": "x"})
        )

    task = asyncio.create_task(answer())
    result = await manager.dispatch(HOST_ID, HostCommand.OBSERVE, session_id="s1")
    await task
    assert result.ok
    assert result.result == {"url": "x"}


async def test_a_refusal_comes_back_as_a_result_not_an_exception() -> None:
    """A login wall is an answer. Modelling it as an error makes it exceptional."""
    manager = BrowserHostManager()
    live, connection = await _attach(manager)

    async def refuse() -> None:
        await asyncio.sleep(0)
        sent = CommandMessage.model_validate_json(connection.sent[-1])
        await manager.handle_result(
            live,
            ResultMessage(
                seq=2,
                command_id=sent.id,
                ok=False,
                error_code=HostErrorCode.USER_HAS_CONTROL,
                error_message="the user is signing in",
            ),
        )

    task = asyncio.create_task(refuse())
    result = await manager.dispatch(HOST_ID, HostCommand.FILL, session_id="s1")
    await task
    assert not result.ok
    assert result.error_code is HostErrorCode.USER_HAS_CONTROL


async def test_dispatch_to_an_absent_host_fails_with_a_recoverable_category() -> None:
    manager = BrowserHostManager()
    with pytest.raises(HostCommandError) as raised:
        await manager.dispatch("nobody", HostCommand.OBSERVE)
    assert raised.value.error_code is HostErrorCode.BACKEND_UNAVAILABLE
    assert raised.value.details["host_id"] == "nobody"


async def test_a_dead_socket_fails_the_command_immediately() -> None:
    manager = BrowserHostManager()
    live, _ = await _attach(manager, FakeConnection(fail=True))
    with pytest.raises(HostCommandError):
        await manager.dispatch(HOST_ID, HostCommand.OBSERVE, session_id="s1")
    assert live.pending == {}


async def test_a_command_that_is_never_answered_times_out_and_is_forgotten() -> None:
    manager = BrowserHostManager(command_timeout=0.05)
    live, _ = await _attach(manager)
    with pytest.raises(HostCommandError) as raised:
        await manager.dispatch(HOST_ID, HostCommand.OBSERVE, session_id="s1")
    assert raised.value.error_code is HostErrorCode.TIMED_OUT
    assert live.pending == {}


async def test_a_late_result_is_dropped_rather_than_raising() -> None:
    """A reply arriving after its timeout is expected, not an error."""
    manager = BrowserHostManager()
    live, _ = await _attach(manager)
    await manager.handle_result(live, ResultMessage(seq=2, command_id="never-sent", ok=True))


async def test_consequential_commands_require_an_idempotency_key() -> None:
    """A click is the submit button. Replaying one submits an application twice."""
    manager = BrowserHostManager()
    await _attach(manager)
    for command in (HostCommand.CLICK, HostCommand.UPLOAD, HostCommand.CREATE_SESSION):
        with pytest.raises(HostCommandError) as raised:
            await manager.dispatch(HOST_ID, command, session_id="s1")
        assert raised.value.error_code is HostErrorCode.MALFORMED


async def test_a_command_carries_a_deadline_the_host_can_enforce() -> None:
    manager = BrowserHostManager(command_timeout=0.05)
    _, connection = await _attach(manager)
    with pytest.raises(HostCommandError):
        await manager.dispatch(HOST_ID, HostCommand.OBSERVE, session_id="s1")
    sent = CommandMessage.model_validate_json(connection.sent[-1])
    # Beyond our own timeout, so a command we abandoned is refused there rather
    # than executed late.
    assert sent.expires_at is not None
    assert sent.expires_at > sent.issued_at


async def test_the_idempotency_key_travels_with_the_command() -> None:
    manager = BrowserHostManager(command_timeout=0.05)
    _, connection = await _attach(manager)
    with pytest.raises(HostCommandError):
        await manager.dispatch(
            HOST_ID, HostCommand.CLICK, session_id="s1", idempotency_key="submit:01JABC"
        )
    assert CommandMessage.model_validate_json(connection.sent[-1]).idempotency_key == (
        "submit:01JABC"
    )


# ---------------------------------------------------------------------------
# Pairing persistence
# ---------------------------------------------------------------------------


async def test_pairing_stores_only_a_hash(database) -> None:
    async with database.session() as session:
        repo = BrowserHostRepository(session)
        paired = await repo.pair(host_id=HOST_ID, display_name="Spencer's Mac")
        assert len(paired.secret) >= 40
        assert paired.record.credential_hash is not None
        assert paired.secret not in paired.record.credential_hash
        assert paired.secret.startswith(paired.record.credential_prefix or "")
        assert verify_host_credential(paired.secret, paired.record.credential_hash)


async def test_authentication_accepts_the_right_secret_only(database) -> None:
    async with database.session() as session:
        repo = BrowserHostRepository(session)
        paired = await repo.pair(host_id=HOST_ID)
        assert await repo.authenticate(host_id=HOST_ID, credential=paired.secret) is not None
        assert await repo.authenticate(host_id=HOST_ID, credential="wrong") is None
        # Unknown host and wrong secret are indistinguishable: a socket that
        # drives a browser must not be an oracle for which hosts exist.
        assert await repo.authenticate(host_id="other", credential=paired.secret) is None


async def test_re_pairing_rotates_the_credential(database) -> None:
    """ "I lost the token" is a supported operation, and the old one stops working."""
    async with database.session() as session:
        repo = BrowserHostRepository(session)
        first = await repo.pair(host_id=HOST_ID)
        second = await repo.pair(host_id=HOST_ID)
        assert first.secret != second.secret
        assert first.record.id == second.record.id
        assert await repo.authenticate(host_id=HOST_ID, credential=first.secret) is None
        assert await repo.authenticate(host_id=HOST_ID, credential=second.secret) is not None


async def test_revocation_stops_authentication_and_keeps_the_record(database) -> None:
    async with database.session() as session:
        repo = BrowserHostRepository(session)
        paired = await repo.pair(host_id=HOST_ID)
        revoked = await repo.revoke(HOST_ID)
        assert revoked is not None
        assert revoked.state is HostConnectionState.REVOKED
        assert await repo.authenticate(host_id=HOST_ID, credential=paired.secret) is None
        # Kept so the revocation is auditable and a host that reappears is
        # identified as revoked rather than as unknown.
        assert await repo.get(HOST_ID) is not None


async def test_revoking_an_unknown_host_reports_rather_than_creating_one(database) -> None:
    async with database.session() as session:
        assert await BrowserHostRepository(session).revoke("nobody") is None


async def test_a_disconnect_keeps_the_sessions_that_may_still_be_resumable(database) -> None:
    async with database.session() as session:
        repo = BrowserHostRepository(session)
        record = (await repo.pair(host_id=HOST_ID)).record
        record.active_sessions = ["s1"]
        record.state = HostConnectionState.CONNECTED
        await repo.save(record)
        await repo.mark_disconnected(HOST_ID, reason="socket closed")
        stored = await repo.get(HOST_ID)
        assert stored is not None
        assert stored.state is HostConnectionState.DISCONNECTED
        # The task space behind an attempt survives a closed socket.
        assert stored.active_sessions == ["s1"]


async def test_startup_clears_connection_states_no_process_can_own(database) -> None:
    async with database.session() as session:
        repo = BrowserHostRepository(session)
        record = (await repo.pair(host_id=HOST_ID)).record
        record.state = HostConnectionState.CONNECTED
        await repo.save(record)
        assert await repo.clear_stale_connection_states() == 1
        stored = await repo.get(HOST_ID)
        assert stored is not None
        assert stored.state is HostConnectionState.DISCONNECTED


async def test_revocation_clears_the_stored_hash(database) -> None:
    """Nothing left in the row can be replayed as a credential."""
    async with database.session() as session:
        repo = BrowserHostRepository(session)
        await repo.pair(host_id=HOST_ID)
        revoked = await repo.revoke(HOST_ID)
        assert revoked is not None
        assert revoked.credential_hash is None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def _client(database) -> TestClient:
    set_container(None)
    set_database(database)
    settings = database.settings.model_copy(update={"security": SecuritySettings(enabled=False)})
    return TestClient(create_app(settings))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LiveApi:
    """A real uvicorn process.

    Starlette's TestClient websocket deadlocks once the handler sends a frame
    and then waits to receive the next one, which is exactly the happy path
    for a registered host. Refusal tests close the socket after the error
    frame and can stay on TestClient; anything that keeps the connection open
    has to talk to a real server.
    """

    def __init__(self, database) -> None:
        set_container(None)
        set_database(database)
        settings = database.settings.model_copy(update={"security": SecuritySettings(enabled=False)})
        self.app = create_app(settings)
        self.port = _free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{WEBSOCKET_PATH}"

    def start(self) -> _LiveApi:
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                return self
            time.sleep(0.05)
        msg = "uvicorn did not start"
        raise RuntimeError(msg)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def pair(self, host_id: str = HOST_ID) -> str:
        response = httpx.post(
            f"{self.http}/api/v1/browser-hosts/pair", json={"host_id": host_id}, timeout=5.0
        )
        response.raise_for_status()
        return str(response.json()["credential"])

    def hosts(self) -> list[dict]:
        response = httpx.get(f"{self.http}/api/v1/browser-hosts", timeout=5.0)
        response.raise_for_status()
        return list(response.json())

    def connect(self):
        return ws_connect(self.ws_url, open_timeout=5, close_timeout=5)


def _send(socket, payload: dict) -> None:
    socket.send(json.dumps(payload))


def _recv(socket) -> dict:
    return json.loads(socket.recv(timeout=5))


def test_pairing_returns_the_credential_exactly_once(database) -> None:
    client = _client(database)
    created = client.post(
        "/api/v1/browser-hosts/pair", json={"host_id": HOST_ID, "display_name": "Mac"}
    )
    assert created.status_code == 201
    body = created.json()
    secret = body["credential"]
    assert secret
    assert body["websocket_path"] == WEBSOCKET_PATH
    assert body["protocol_version"] == PROTOCOL_VERSION

    listed = client.get("/api/v1/browser-hosts").json()
    assert len(listed) == 1
    # The list view carries a prefix for telling tokens apart, never the secret.
    assert secret not in str(listed)
    assert listed[0]["credential_prefix"]
    assert listed[0]["connected"] is False


def test_a_pairing_id_must_look_like_an_identifier(database) -> None:
    client = _client(database)
    for bad in ("", "has spaces", "../../etc/passwd", "a" * 200):
        assert client.post("/api/v1/browser-hosts/pair", json={"host_id": bad}).status_code == 422


def test_revoking_an_unknown_host_is_a_404(database) -> None:
    assert _client(database).post("/api/v1/browser-hosts/nobody/revoke").status_code == 404


def test_a_host_with_no_credential_is_rejected(database) -> None:
    client = _client(database)
    with client.websocket_connect(WEBSOCKET_PATH) as socket:
        socket.send_json(_registration(credential="not-a-real-credential").model_dump())
        error = ErrorMessage.model_validate(socket.receive_json())
    assert error.code is HostErrorCode.UNAUTHENTICATED


def test_a_revoked_host_cannot_reconnect(database) -> None:
    client = _client(database)
    secret = client.post("/api/v1/browser-hosts/pair", json={"host_id": HOST_ID}).json()[
        "credential"
    ]
    client.post(f"/api/v1/browser-hosts/{HOST_ID}/revoke")
    with client.websocket_connect(WEBSOCKET_PATH) as socket:
        socket.send_json(_registration(credential=secret).model_dump())
        error = ErrorMessage.model_validate(socket.receive_json())
    assert error.code is HostErrorCode.UNAUTHENTICATED


def test_an_incompatible_protocol_is_refused(database) -> None:
    client = _client(database)
    secret = client.post("/api/v1/browser-hosts/pair", json={"host_id": HOST_ID}).json()[
        "credential"
    ]
    with client.websocket_connect(WEBSOCKET_PATH) as socket:
        socket.send_json(
            _registration(credential=secret, protocol_version=PROTOCOL_VERSION + 5).model_dump()
        )
        error = ErrorMessage.model_validate(socket.receive_json())
    assert error.code is HostErrorCode.PROTOCOL_INCOMPATIBLE


def test_the_first_frame_must_be_a_registration(database) -> None:
    client = _client(database)
    with client.websocket_connect(WEBSOCKET_PATH) as socket:
        socket.send_json(HeartbeatMessage(seq=1).model_dump())
        error = ErrorMessage.model_validate(socket.receive_json())
    assert error.code is HostErrorCode.MALFORMED


def test_a_paired_host_is_accepted_and_becomes_connected(database) -> None:
    api = _LiveApi(database).start()
    try:
        secret = api.pair()
        with api.connect() as socket:
            _send(socket, _registration(credential=secret).model_dump())
            accepted = RegisteredMessage.model_validate(_recv(socket))
            assert accepted.protocol_version == PROTOCOL_VERSION
            assert accepted.host_record_id
            listed = api.hosts()
            assert listed[0]["connected"] is True
            assert listed[0]["platform"] == "darwin"
            assert {b["slug"] for b in listed[0]["backends"]} == {"ego_lite", "playwright"}
        assert api.hosts()[0]["connected"] is False
    finally:
        api.stop()


def test_a_malformed_frame_does_not_drop_a_live_connection(database) -> None:
    """The connection may be holding a half-finished application."""
    api = _LiveApi(database).start()
    try:
        secret = api.pair()
        with api.connect() as socket:
            _send(socket, _registration(credential=secret).model_dump())
            _recv(socket)
            socket.send("not json at all")
            error = ErrorMessage.model_validate(_recv(socket))
            assert error.code is HostErrorCode.MALFORMED
            _send(socket, HeartbeatMessage(seq=2, open_sessions=["s1"]).model_dump())
            time.sleep(0.1)
            assert api.hosts()[0]["active_sessions"] == ["s1"]
    finally:
        api.stop()


def test_a_replayed_frame_is_refused_on_the_wire(database) -> None:
    api = _LiveApi(database).start()
    try:
        secret = api.pair()
        with api.connect() as socket:
            _send(socket, _registration(credential=secret, seq=1).model_dump())
            _recv(socket)
            _send(socket, HeartbeatMessage(seq=2).model_dump())
            _send(socket, HeartbeatMessage(seq=2).model_dump())
            error = ErrorMessage.model_validate(_recv(socket))
        assert error.code is HostErrorCode.REPLAYED
    finally:
        api.stop()


def test_re_registering_on_a_live_connection_is_refused(database) -> None:
    api = _LiveApi(database).start()
    try:
        secret = api.pair()
        with api.connect() as socket:
            _send(socket, _registration(credential=secret, seq=1).model_dump())
            _recv(socket)
            _send(socket, _registration(credential=secret, seq=2).model_dump())
            error = ErrorMessage.model_validate(_recv(socket))
        assert error.code is HostErrorCode.MALFORMED
    finally:
        api.stop()


def test_a_reconnecting_host_reclaims_its_own_record(database) -> None:
    """Otherwise every restart would accumulate another row."""
    api = _LiveApi(database).start()
    try:
        secret = api.pair()
        for _ in range(3):
            with api.connect() as socket:
                _send(socket, _registration(credential=secret).model_dump())
                _recv(socket)
        assert len(api.hosts()) == 1
    finally:
        api.stop()


def test_resumable_sessions_survive_a_reconnect(database) -> None:
    api = _LiveApi(database).start()
    try:
        secret = api.pair()
        with api.connect() as socket:
            _send(
                socket,
                _registration(credential=secret, resumable_sessions=["s1", "s2"]).model_dump(),
            )
            accepted = RegisteredMessage.model_validate(_recv(socket))
            assert accepted.expected_sessions == ["s1", "s2"]
    finally:
        api.stop()


def test_the_human_api_still_needs_a_session_when_auth_is_on(database) -> None:
    """The host socket's exemption must not extend to the REST routes."""
    set_container(None)
    set_database(database)
    settings = database.settings.model_copy(
        update={"security": SecuritySettings(enabled=True, password=SecretStr("a-real-password"))}
    )
    client = TestClient(create_app(settings))
    assert client.get("/api/v1/browser-hosts").status_code == 401
    assert client.post("/api/v1/browser-hosts/pair", json={"host_id": HOST_ID}).status_code == 401
