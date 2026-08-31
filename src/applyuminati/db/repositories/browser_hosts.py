"""Browser Host pairing records.

Authentication happens here, in :meth:`BrowserHostRepository.authenticate`, and
it is the only place a presented credential is checked. Keeping it in one method
means the rules a host must satisfy are readable as a list rather than spread
across a router: the host must exist, hold a credential, not be revoked, and
present a secret matching the stored hash.

Lookup is by ``host_id`` and the hash is compared in constant time, rather than
querying by ``credential_hash``. Querying by the secret's hash would let a
caller enumerate valid credentials by watching which lookups return a row, and
would authenticate a host whose id did not match the credential it presented.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.clock import utcnow
from applyuminati.core.models.browser_host import (
    BrowserHostBackend,
    BrowserHostRecord,
    HostConnectionState,
)
from applyuminati.core.security import (
    MintedCredential,
    mint_host_credential,
    verify_host_credential,
)
from applyuminati.db.models import BrowserHostRow

__all__ = ["BrowserHostRepository", "PairedHost"]


class PairedHost:
    """A freshly paired host and the secret it will need, shown once.

    A pair, not a record with a secret field, so the secret cannot accidentally
    be persisted or serialised alongside the record it belongs to.
    """

    __slots__ = ("record", "secret")

    def __init__(self, record: BrowserHostRecord, secret: str) -> None:
        self.record = record
        self.secret = secret


def _to_record(row: BrowserHostRow) -> BrowserHostRecord:
    return BrowserHostRecord(
        id=row.id,
        host_id=row.host_id,
        display_name=row.display_name,
        platform=row.platform,
        architecture=row.architecture,
        host_version=row.host_version,
        protocol_version=row.protocol_version,
        state=HostConnectionState(row.state),
        backends=[BrowserHostBackend.model_validate(entry) for entry in row.backends or []],
        credential_hash=row.credential_hash,
        credential_prefix=row.credential_prefix,
        credential_issued_at=row.credential_issued_at,
        revoked_at=row.revoked_at,
        paired_at=row.paired_at,
        last_seen_at=row.last_seen_at,
        last_connected_at=row.last_connected_at,
        last_error=row.last_error,
        active_sessions=list(row.active_sessions or []),
    )


def _apply(record: BrowserHostRecord, row: BrowserHostRow) -> BrowserHostRow:
    row.host_id = record.host_id
    row.display_name = record.display_name
    row.platform = record.platform
    row.architecture = record.architecture
    row.host_version = record.host_version
    row.protocol_version = record.protocol_version
    row.state = record.state.value
    row.backends = [entry.model_dump(mode="json") for entry in record.backends]
    row.credential_hash = record.credential_hash
    row.credential_prefix = record.credential_prefix
    row.credential_issued_at = record.credential_issued_at
    row.revoked_at = record.revoked_at
    row.paired_at = record.paired_at
    row.last_seen_at = record.last_seen_at
    row.last_connected_at = record.last_connected_at
    row.last_error = record.last_error
    row.active_sessions = list(record.active_sessions)
    return row


class BrowserHostRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def pair(self, *, host_id: str, display_name: str | None = None) -> PairedHost:
        """Mint a credential for ``host_id``, replacing any previous one.

        Re-pairing an existing host rotates its credential rather than creating a
        second record, so "I lost the token" is a supported operation and the old
        secret stops working the moment a new one is issued.
        """
        minted: MintedCredential = mint_host_credential()
        row = await self._row_for(host_id)
        if row is None:
            row = BrowserHostRow(host_id=host_id)
            self._session.add(row)
        row.display_name = display_name or row.display_name
        row.credential_hash = minted.hashed
        row.credential_prefix = minted.prefix
        row.credential_issued_at = utcnow()
        row.revoked_at = None
        row.state = HostConnectionState.REGISTERED.value
        row.last_error = None
        await self._session.flush()
        return PairedHost(_to_record(row), minted.secret)

    async def authenticate(self, *, host_id: str, credential: str) -> BrowserHostRecord | None:
        """Return the record when the credential is valid, else ``None``.

        One return value for every kind of failure. An unknown host, a revoked
        host and a wrong secret are indistinguishable to the caller on purpose:
        a connection that controls someone's authenticated browser should not
        also be an oracle for which host ids exist.
        """
        row = await self._row_for(host_id)
        if row is None or row.credential_hash is None or row.revoked_at is not None:
            return None
        if not verify_host_credential(credential, row.credential_hash):
            return None
        return _to_record(row)

    async def revoke(self, host_id: str) -> BrowserHostRecord | None:
        """Withdraw a credential. The host cannot reconnect afterwards.

        The record is kept rather than deleted, so a revocation is auditable and
        a host that reappears is identified as revoked rather than as unknown.
        """
        row = await self._row_for(host_id)
        if row is None:
            return None
        row.revoked_at = utcnow()
        row.state = HostConnectionState.REVOKED.value
        row.credential_hash = None
        row.active_sessions = []
        await self._session.flush()
        return _to_record(row)

    async def save(self, record: BrowserHostRecord) -> BrowserHostRecord:
        row = await self._row_for(record.host_id)
        if row is None:
            row = BrowserHostRow(id=record.id, host_id=record.host_id)
            self._session.add(row)
        _apply(record, row)
        await self._session.flush()
        return record

    async def get(self, host_id: str) -> BrowserHostRecord | None:
        row = await self._row_for(host_id)
        return _to_record(row) if row else None

    async def list(
        self, *, states: Sequence[HostConnectionState] | None = None
    ) -> list[BrowserHostRecord]:
        statement = select(BrowserHostRow)
        if states:
            statement = statement.where(BrowserHostRow.state.in_([s.value for s in states]))
        rows = (
            await self._session.scalars(statement.order_by(BrowserHostRow.paired_at.desc()))
        ).all()
        return [_to_record(row) for row in rows]

    async def mark_disconnected(self, host_id: str, *, reason: str | None = None) -> None:
        """Record a disconnect. Sessions are kept: they may still be resumable.

        A host that closed its socket has not necessarily lost its browser. The
        task space behind an attempt survives, so clearing ``active_sessions``
        here would throw away the identity needed to resume.
        """
        row = await self._row_for(host_id)
        if row is None:
            return
        row.state = HostConnectionState.DISCONNECTED.value
        row.last_error = reason
        await self._session.flush()

    async def clear_stale_connection_states(self) -> int:
        """Reset ``connected``/``stale`` rows at startup.

        A process cannot inherit a connection. Any row claiming to be connected
        when this runs is a leftover from a previous process, and leaving it would
        make the UI promise a browser that is not there.
        """
        rows = (
            await self._session.scalars(
                select(BrowserHostRow).where(
                    BrowserHostRow.state.in_(
                        [HostConnectionState.CONNECTED.value, HostConnectionState.STALE.value]
                    )
                )
            )
        ).all()
        for row in rows:
            row.state = HostConnectionState.DISCONNECTED.value
        await self._session.flush()
        return len(rows)

    async def _row_for(self, host_id: str) -> BrowserHostRow | None:
        return (
            await self._session.scalars(
                select(BrowserHostRow).where(BrowserHostRow.host_id == host_id)
            )
        ).first()
