"""The Browser Host wire protocol.

One rule shapes everything here: **the server asks for browser operations, never
for host actions.** There is no command that runs a shell, reads a file, or
evaluates arbitrary Node on the desktop. The command set is a closed enum, every
payload is a validated model, and a message that does not parse is refused rather
than interpreted generously.

That restraint is the whole point of moving the browser out of the container. A
Browser Host runs on the machine holding the user's real, signed-in browser. A
protocol that could ask it to "run this script" would be a remote shell into the
most sensitive machine in the deployment, reachable by whatever compromises the
server. So the host's obedience is bounded by this file: even a fully
compromised server can only drive a browser, and only through operations the
host already knows how to perform.

``evaluate`` is the one command that looks like an exception and is not. It
exists because both local backends already expose JavaScript evaluation for
control scanning, it runs inside the page rather than on the host, and it is
gated by the ``javascript_eval`` capability. Page-scoped script is the same
authority a bookmarklet has; host-scoped script is not, and is absent.

Framing:

* Every message carries ``type`` and a monotonic ``seq`` per direction.
* Commands carry an ``id``; results echo it in ``command_id``. Unmatched results
  are dropped, which is what makes a late reply from a timed-out command
  harmless instead of confusing.
* Commands carry ``issued_at`` and ``expires_at``. A host refuses an expired
  command, so a message delayed by a reconnect cannot land as a fresh
  instruction on a page that has since moved on.
* Consequential commands carry an ``idempotency_key``. A host that has already
  executed that key returns the recorded result instead of doing it twice, which
  is what keeps a reconnect from submitting an application a second time.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid

__all__ = [
    "CONSEQUENTIAL_COMMANDS",
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "MAX_MESSAGE_BYTES",
    "MIN_PROTOCOL_VERSION",
    "PROTOCOL_VERSION",
    "WEBSOCKET_PATH",
    "CommandMessage",
    "ErrorMessage",
    "EventMessage",
    "HostCommand",
    "HostErrorCode",
    "HostEvent",
    "MessageType",
    "RegisterMessage",
    "RegisteredMessage",
    "ResultMessage",
    "decode_message",
    "protocol_compatible",
]

#: Bumped for any breaking change to framing or command semantics.
PROTOCOL_VERSION = 1
#: Oldest host protocol this server still accepts. Equal to the current version
#: today; kept separate so widening compatibility later is a one-line change
#: rather than an argument about what the version check meant.
MIN_PROTOCOL_VERSION = 1

#: Hosts send a heartbeat this often. The staleness window in
#: :mod:`applyuminati.core.models.browser_host` is much longer, because a laptop
#: that slept is not a laptop that left.
HEARTBEAT_INTERVAL_SECONDS = 30

#: Refused above this size. A browser observation is a few hundred kilobytes of
#: page text at most; anything larger is a bug or an attempt to exhaust memory.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024

DEFAULT_COMMAND_TIMEOUT_SECONDS = 60.0

#: The single endpoint a host connects to. Part of the protocol rather than of
#: the router, so the CLI can print a working command line without the cli layer
#: importing the api layer.
WEBSOCKET_PATH = "/api/v1/browser-hosts/ws"


class MessageType(StrEnum):
    #: host -> server, first message on every connection
    REGISTER = "register"
    #: server -> host, accepting the registration
    REGISTERED = "registered"
    #: server -> host
    COMMAND = "command"
    #: host -> server, answering exactly one command
    RESULT = "result"
    #: host -> server, unsolicited (the user took the browser, a tab closed)
    EVENT = "event"
    #: host -> server
    HEARTBEAT = "heartbeat"
    #: either direction, terminal for the connection
    ERROR = "error"


class HostCommand(StrEnum):
    """Everything the server may ask a host to do. Nothing else is possible.

    Semantic browser operations only. No shell, no filesystem, no host-scoped
    script. Adding a member here is a deliberate widening of what a compromised
    server could do to the user's machine, and should be argued for on that
    basis.
    """

    # -- sessions
    #: Params: ``backend``, and optionally ``session_id`` and ``task_space`` so
    #: the caller's durable identity names the workspace instead of the host
    #: inventing one. Result: ``session_id``, ``backend``, ``task_space_id``.
    CREATE_SESSION = "create_session"
    CLOSE_SESSION = "close_session"
    CHECKPOINT = "checkpoint"

    # -- navigation and reading
    NAVIGATE = "navigate"
    OBSERVE = "observe"
    SCREENSHOT = "screenshot"
    WAIT_FOR_NAVIGATION = "wait_for_navigation"

    # -- interaction
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    SET_CHECKED = "set_checked"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    #: Page-scoped, capability-gated. Runs in the tab, not on the host.
    EVALUATE = "evaluate"

    # -- tabs
    OPEN_TAB = "open_tab"
    CLOSE_TAB = "close_tab"
    ACTIVATE_TAB = "activate_tab"
    LIST_TABS = "list_tabs"

    # -- ownership
    REQUEST_HANDOFF = "request_handoff"
    RECLAIM_CONTROL = "reclaim_control"
    CONTROL_STATE = "control_state"

    # -- housekeeping
    HEALTH = "health"
    CANCEL = "cancel"


#: Commands whose repetition is not free. A host must deduplicate these by
#: ``idempotency_key``.
#:
#: ``CLICK`` is here because the final submit button is a click, and a click
#: replayed after a reconnect is how an application gets submitted twice. Erring
#: toward treating clicks as consequential costs a dictionary lookup; erring the
#: other way costs a duplicate application to an employer.
CONSEQUENTIAL_COMMANDS: frozenset[HostCommand] = frozenset(
    {
        HostCommand.CLICK,
        HostCommand.UPLOAD,
        HostCommand.CREATE_SESSION,
    }
)


class HostEvent(StrEnum):
    """Things a host reports without being asked."""

    #: The user took the browser. Sent whether or not we asked them to.
    CONTROL_CHANGED = "control_changed"
    #: The page changed underneath us: a redirect, a session timeout.
    NAVIGATION = "navigation"
    #: A backend became available or stopped being available.
    BACKEND_CHANGED = "backend_changed"
    #: A session ended outside our control: the user closed the tab.
    SESSION_CLOSED = "session_closed"
    #: The host is shutting down cleanly, so its sessions can be marked resumable
    #: rather than failed.
    SHUTTING_DOWN = "shutting_down"


class HostErrorCode(StrEnum):
    """Why a command or a connection was refused. A code, not prose."""

    UNAUTHENTICATED = "unauthenticated"
    REVOKED = "revoked"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    MALFORMED = "malformed"
    MESSAGE_TOO_LARGE = "message_too_large"
    UNKNOWN_COMMAND = "unknown_command"
    UNKNOWN_SESSION = "unknown_session"
    #: The command's own deadline passed before the host got to it.
    EXPIRED = "expired"
    #: A duplicate ``seq``, i.e. a replayed frame.
    REPLAYED = "replayed"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    #: The session is currently owned by the user; the agent must not act.
    USER_HAS_CONTROL = "user_has_control"
    INTERNAL = "internal"


class BackendAdvertisement(BaseModel):
    """One backend, as the host describes it at registration."""

    model_config = ConfigDict(extra="forbid")

    available: bool = False
    preferred: bool = False
    version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    detail: str | None = None


class RegisterMessage(BaseModel):
    """First frame from a host. Carries its credential and its capabilities.

    The credential travels in the message rather than a header so the same frame
    works over any transport, and so a host that reconnects re-proves itself
    every time instead of relying on a connection someone else may have opened.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal[MessageType.REGISTER] = MessageType.REGISTER
    seq: int = 0
    protocol_version: int = PROTOCOL_VERSION
    host_id: str = Field(min_length=1, max_length=128)
    #: Never logged, never stored. Compared against a stored hash and discarded.
    credential: str = Field(min_length=1, max_length=512, repr=False)
    display_name: str | None = Field(default=None, max_length=128)
    platform: str | None = Field(default=None, max_length=32)
    architecture: str | None = Field(default=None, max_length=32)
    host_version: str | None = Field(default=None, max_length=32)
    backends: dict[str, BackendAdvertisement] = Field(default_factory=dict)
    #: Sessions the host still holds, so a reconnect can resume an attempt
    #: instead of orphaning it.
    resumable_sessions: list[str] = Field(default_factory=list)


class RegisteredMessage(BaseModel):
    """Server's acceptance. Tells the host what this server expects of it."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[MessageType.REGISTERED] = MessageType.REGISTERED
    seq: int = 0
    protocol_version: int = PROTOCOL_VERSION
    host_record_id: str
    heartbeat_interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS
    #: Sessions the server believes are open on this host. A host that disagrees
    #: reports the difference rather than the server assuming either way.
    expected_sessions: list[str] = Field(default_factory=list)


class CommandMessage(BaseModel):
    """One instruction. Bounded in scope, in time, and in repetition."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[MessageType.COMMAND] = MessageType.COMMAND
    seq: int = 0
    id: str = Field(default_factory=new_ulid)
    command: HostCommand
    #: Absent only for session-independent commands (``health``).
    session_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    issued_at: float = Field(default_factory=lambda: utcnow().timestamp())
    #: Absolute deadline. A host past this refuses rather than executing, so a
    #: command delayed by a reconnect cannot act on a page that has moved on.
    expires_at: float | None = None
    #: Required for CONSEQUENTIAL_COMMANDS. A host that has seen this key returns
    #: its recorded result rather than acting again.
    idempotency_key: str | None = None

    def expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else utcnow().timestamp()) >= self.expires_at


class ResultMessage(BaseModel):
    """Answer to exactly one command.

    ``ok=False`` with an ``error_code`` is a normal outcome, not an exception. A
    login wall, a validation error and a user holding the browser are all
    expected answers, and modelling them as failures to be raised would make the
    ordinary path exceptional.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal[MessageType.RESULT] = MessageType.RESULT
    seq: int = 0
    command_id: str
    ok: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: HostErrorCode | None = None
    error_message: str | None = None
    duration_ms: float | None = None
    #: True when this is a recorded reply replayed for a repeated
    #: ``idempotency_key`` rather than a fresh execution.
    deduplicated: bool = False


class EventMessage(BaseModel):
    """Unsolicited report. The user acting is the main reason this exists."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[MessageType.EVENT] = MessageType.EVENT
    seq: int = 0
    event: HostEvent
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: float = Field(default_factory=lambda: utcnow().timestamp())


class HeartbeatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[MessageType.HEARTBEAT] = MessageType.HEARTBEAT
    seq: int = 0
    #: Sessions the host still holds. Cheap reconciliation on every beat, so a
    #: server that restarted rebuilds its picture without a special handshake.
    open_sessions: list[str] = Field(default_factory=list)


class ErrorMessage(BaseModel):
    """Terminal for the connection. Carries a code, never a stack trace."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[MessageType.ERROR] = MessageType.ERROR
    seq: int = 0
    code: HostErrorCode
    message: str = ""


#: Inbound frame types, for dispatch. Server-to-host frames are constructed
#: locally and never parsed from the wire here.
InboundMessage = RegisterMessage | ResultMessage | EventMessage | HeartbeatMessage | ErrorMessage

_INBOUND: dict[str, type[BaseModel]] = {
    MessageType.REGISTER.value: RegisterMessage,
    MessageType.RESULT.value: ResultMessage,
    MessageType.EVENT.value: EventMessage,
    MessageType.HEARTBEAT.value: HeartbeatMessage,
    MessageType.ERROR.value: ErrorMessage,
}


def protocol_compatible(version: int | None) -> bool:
    """Whether this server can talk to a host claiming ``version``.

    A newer host is refused rather than tolerated. Guessing at framing we do not
    know, against a process that drives someone's authenticated browser, is not
    a risk worth taking to avoid an upgrade prompt.
    """
    if version is None:
        return False
    return MIN_PROTOCOL_VERSION <= version <= PROTOCOL_VERSION


class ProtocolError(Exception):
    """A frame that could not be accepted. Carries a code for the wire."""

    def __init__(self, code: HostErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def decode_message(raw: str | bytes) -> InboundMessage:
    """Parse and validate one inbound frame.

    Strict: unknown types and unknown fields are refused. A host speaking
    something this server does not fully understand is a host to reject, not to
    interpret optimistically, because the cost of a misread instruction here is
    an action taken in a real person's browser.
    """
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            HostErrorCode.MESSAGE_TOO_LARGE,
            f"frame is {len(encoded)} bytes; the limit is {MAX_MESSAGE_BYTES}",
        )
    try:
        payload = json.loads(encoded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(HostErrorCode.MALFORMED, f"not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(HostErrorCode.MALFORMED, "frame must be a JSON object")

    model = _INBOUND.get(str(payload.get("type")))
    if model is None:
        raise ProtocolError(
            HostErrorCode.MALFORMED, f"unknown message type {payload.get('type')!r}"
        )
    try:
        return model.model_validate(payload)  # type: ignore[return-value]
    except Exception as exc:
        raise ProtocolError(HostErrorCode.MALFORMED, f"invalid {model.__name__}: {exc}") from exc
