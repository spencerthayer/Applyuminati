"""Memory store: reinforce, contradict, supersede — never overwrite facts.

The store wraps :class:`MemoryRepository` and adds the policy layer:

* Reinforcing an existing ``(kind, scope, key)`` bumps
  ``supporting_observations`` instead of duplicating the row.
* Contradicting bumps ``contradicting_observations`` and supersedes the record
  once contradictions outweigh support.
* **A machine-driven write at ``VERIFIED`` or ``USER_APPROVED`` is refused**
  unless ``by_user=True`` is passed. This is the hard rule that enforces
  "an LLM must never silently promote an inference into a verified fact."
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.models.memory import MemoryKind, MemoryRecord
from applyuminati.core.provenance import AssertionLevel, Provenance, can_auto_promote
from applyuminati.db.repositories.memory import MemoryRepository

__all__ = ["MemoryStore"]


class MemoryStore:
    def __init__(self, repo: MemoryRepository) -> None:
        self._repo = repo

    async def remember(
        self,
        kind: MemoryKind,
        scope: str,
        key: str,
        content: str,
        *,
        data: dict[str, Any] | None = None,
        level: AssertionLevel = AssertionLevel.INFERRED,
        provenance: list[Provenance] | None = None,
        ttl_seconds: int | None = None,
        by_user: bool = False,
    ) -> MemoryRecord:
        """Record or reinforce a lesson.

        Raises ``ValueError`` when a machine attempts to write at
        ``VERIFIED`` or ``USER_APPROVED`` without ``by_user=True``.
        """
        if not by_user and not can_auto_promote(AssertionLevel.INFERRED, level):
            msg = (
                f"refusing machine-driven memory write at {level}; "
                "this level requires explicit user approval (by_user=True)"
            )
            raise ValueError(msg)

        existing = await self._repo.find(kind, scope, key)
        if existing is not None:
            existing.supporting_observations += 1
            existing.content = content
            existing.data = data or existing.data
            existing.updated_at = utcnow()
            if ttl_seconds is not None:
                existing.expires_at = utcnow() + timedelta(seconds=ttl_seconds)
            return await self._repo.upsert(existing)

        expires_at = utcnow() + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
        record = MemoryRecord(
            id=new_ulid(),
            kind=kind,
            scope=scope,
            key=key,
            content=content,
            data=data or {},
            level=level,
            provenance=provenance or [],
            expires_at=expires_at,
        )
        return await self._repo.upsert(record)

    async def contradict(
        self, kind: MemoryKind, scope: str, key: str, reason: str
    ) -> MemoryRecord | None:
        """Record evidence against a lesson; supersede when it tips negative."""
        existing = await self._repo.find(kind, scope, key)
        if existing is None:
            return None
        existing.contradicting_observations += 1
        existing.updated_at = utcnow()
        if existing.confidence < 0.5:
            replacement = MemoryRecord(
                id=new_ulid(),
                kind=kind,
                scope=scope,
                key=key,
                content=f"[superseded] {existing.content}",
                data={**existing.data, "contradiction_reason": reason},
                level=AssertionLevel.INFERRED,
                provenance=existing.provenance,
            )
            replacement = await self._repo.upsert(replacement)
            existing.superseded_by = replacement.id
            await self._repo.upsert(existing)
            return replacement
        return await self._repo.upsert(existing)

    async def recall(
        self,
        kind: MemoryKind,
        *,
        scope: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        records = await self._repo.search(kind=kind, scope=scope, active_only=True, limit=limit * 2)
        filtered = [r for r in records if r.confidence >= min_confidence]
        filtered.sort(key=lambda r: (r.confidence, r.updated_at), reverse=True)
        return filtered[:limit]

    async def supersede(self, record_id: str, replacement: MemoryRecord) -> MemoryRecord:
        """Mark ``record_id`` as superseded by ``replacement``.

        Nothing is hard-deleted: the old record stays with
        ``superseded_by`` set, so learning remains reversible.
        """
        replacement = await self._repo.upsert(replacement)
        # We need to mark the old record. Since we don't have a direct
        # "get by id" on the repo, search for it.
        records = await self._repo.search(
            kind=replacement.kind, scope=replacement.scope, active_only=False, limit=200
        )
        for record in records:
            if record.id == record_id and record.superseded_by is None:
                record.superseded_by = replacement.id
                await self._repo.upsert(record)
                break
        return replacement
