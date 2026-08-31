"""Browser Hosts: the machines that actually own a browser.

Applyuminati's production image is Linux. ego lite is a macOS desktop
application. There is no arrangement of Docker flags that resolves that: the
container cannot use the user's Mac browser, and the alternatives are all worse
than the problem. Containerising ego lite is not possible, mounting host
executables into the container is a sandbox escape wearing a volume mount, and
letting the server run arbitrary Node on the desktop is a remote shell with
extra steps.

So the browser moves out. A small native process, ``applyuminati-browser-host``,
runs where the browser lives and connects *outward* to Applyuminati. The server
sends semantic commands; the host executes them against whichever backend it
has. Nothing in this model implies Docker, a LAN, or one machine: a host may be
the same laptop, a Mac talking to a NAS, or eventually a Windows box.

Outbound is the load-bearing direction. If the server had to dial the desktop it
would need to discover an address that changes with every network, and every
deployment would need a listening port on the machine holding the user's
authenticated browser. A host that dials out works behind NAT, needs no inbound
firewall rule, and is the party that decides to participate.

This module is the durable record. The wire protocol is
:mod:`applyuminati.browser.host_protocol`; the connection registry is
:mod:`applyuminati.browser.host_manager`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid

__all__ = [
    "STALE_AFTER",
    "BrowserHostBackend",
    "BrowserHostRecord",
    "HostConnectionState",
]

#: A host that has not spoken for this long is presumed gone. Longer than the
#: heartbeat interval by a wide margin: a laptop that slept through a heartbeat
#: has not been decommissioned, and marking it offline would abandon an
#: application attempt that could still be resumed.
STALE_AFTER = timedelta(minutes=5)


class HostConnectionState(StrEnum):
    """Whether a host can be given work right now."""

    #: Paired, never connected. The credential exists and is waiting.
    REGISTERED = "registered"
    CONNECTED = "connected"
    #: Connected recently, silent since. Distinct from disconnected because the
    #: attempt it was running is still resumable when it comes back.
    STALE = "stale"
    DISCONNECTED = "disconnected"
    #: Credential withdrawn. Reconnection is refused.
    REVOKED = "revoked"
    #: Speaks a protocol version this server cannot talk to.
    INCOMPATIBLE = "incompatible"


class BrowserHostBackend(BaseModel):
    """One browser implementation a host reports.

    Capability strings rather than the enum, deliberately. A host may run a newer
    build that knows a capability this server does not, and rejecting the whole
    registration over an unrecognised string would make every server upgrade a
    flag day. Unknown capabilities are stored, ignored by matching, and visible
    in the UI.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str
    available: bool = False
    #: The host's own opinion of which of its backends to use. Advisory: the
    #: server's capability matching decides, because only the server knows what
    #: the application needs.
    preferred: bool = False
    version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    #: Why an unavailable backend is unavailable, for the diagnosis a user needs.
    detail: str | None = None


class BrowserHostRecord(BaseModel):
    """A paired Browser Host.

    Only what is needed to route work and diagnose a failure. No serial numbers,
    no user names, no installed-software inventory: this record is visible in the
    UI and in logs, and a job-application tool has no business fingerprinting the
    machine it runs on.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: Host-chosen stable identifier, e.g. ``spencers-mac``. Scoped to this
    #: install and used for reconnection, so a restarted host resumes its own
    #: record instead of accumulating duplicates.
    host_id: str
    display_name: str | None = None
    platform: str | None = None
    architecture: str | None = None
    #: Version of the browser-host process.
    host_version: str | None = None
    #: Wire protocol version it speaks.
    protocol_version: int | None = None

    state: HostConnectionState = HostConnectionState.REGISTERED
    backends: list[BrowserHostBackend] = Field(default_factory=list)

    #: SHA-256 of the pairing credential. The secret is shown once at pairing and
    #: never stored, so a copy of this database does not let anyone drive the
    #: user's browser.
    credential_hash: str | None = None
    #: Leading characters of the secret, so a user can tell two credentials apart
    #: when revoking one.
    credential_prefix: str | None = None
    credential_issued_at: datetime | None = None
    revoked_at: datetime | None = None

    paired_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime | None = None
    last_connected_at: datetime | None = None
    #: One line explaining the current state: a version mismatch, a probe failure.
    last_error: str | None = None
    #: Browser session ids currently open on this host, so a reconnect can be
    #: matched against attempts waiting for it.
    active_sessions: list[str] = Field(default_factory=list)

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def paired(self) -> bool:
        """True when a credential exists and has not been withdrawn."""
        return self.credential_hash is not None and not self.revoked

    def capabilities(self, *, backend: str | None = None) -> frozenset[str]:
        """Capabilities across available backends, or one named backend.

        Union rather than intersection: the host runs whichever backend the
        server picks, so what matters is whether *some* backend can do the work.
        """
        return frozenset(
            capability
            for entry in self.backends
            if entry.available and (backend is None or entry.slug == backend)
            for capability in entry.capabilities
        )

    def backend(self, slug: str) -> BrowserHostBackend | None:
        return next((b for b in self.backends if b.slug == slug), None)

    def available_backends(self) -> list[BrowserHostBackend]:
        """Available backends, the host's preference first."""
        return sorted(
            (b for b in self.backends if b.available),
            key=lambda b: (not b.preferred, b.slug),
        )

    def is_stale(self, *, now: datetime | None = None, after: timedelta = STALE_AFTER) -> bool:
        if self.state is not HostConnectionState.CONNECTED:
            return False
        if self.last_seen_at is None:
            return True
        return (now or utcnow()) - self.last_seen_at > after

    def usable(self) -> bool:
        """Whether the server may dispatch a command to this host now."""
        return (
            self.paired
            and self.state is HostConnectionState.CONNECTED
            and any(b.available for b in self.backends)
        )
