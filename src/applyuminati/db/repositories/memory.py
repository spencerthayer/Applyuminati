"""Memory, learning-signal and outcome persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.models.memory import (
    LearningSignal,
    MemoryKind,
    MemoryRecord,
    OutcomeRecord,
)
from applyuminati.db.mappers import (
    memory_to_row,
    outcome_to_row,
    row_to_memory,
    row_to_outcome,
    row_to_signal,
    signal_to_row,
)
from applyuminati.db.models import LearningSignalRow, MemoryRow, OutcomeRow


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, record: MemoryRecord) -> MemoryRecord:
        row = await self._session.get(MemoryRow, record.id)
        if row is None:
            row = MemoryRow(id=record.id)
            self._session.add(row)
        memory_to_row(record, row=row)
        await self._session.flush()
        return record

    async def find(self, kind: MemoryKind, scope: str, key: str) -> MemoryRecord | None:
        row = (
            await self._session.scalars(
                select(MemoryRow)
                .where(
                    MemoryRow.kind == kind.value,
                    MemoryRow.scope == scope,
                    MemoryRow.key == key,
                )
                .order_by(MemoryRow.updated_at.desc())
                .limit(1)
            )
        ).first()
        return row_to_memory(row) if row else None

    async def search(
        self,
        *,
        kind: MemoryKind | None = None,
        scope: str | None = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        statement = select(MemoryRow)
        if kind is not None:
            statement = statement.where(MemoryRow.kind == kind.value)
        if scope is not None:
            statement = statement.where(MemoryRow.scope == scope)
        if active_only:
            statement = statement.where(MemoryRow.superseded_by.is_(None))
        statement = statement.order_by(MemoryRow.updated_at.desc()).limit(limit)
        rows = (await self._session.scalars(statement)).all()
        return [row_to_memory(row) for row in rows]

    async def record_signal(self, signal: LearningSignal) -> LearningSignal:
        self._session.add(signal_to_row(signal))
        await self._session.flush()
        return signal

    async def signals_for(self, artifact_id: str) -> list[LearningSignal]:
        rows = await self._session.scalars(
            select(LearningSignalRow)
            .where(LearningSignalRow.artifact_id == artifact_id)
            .order_by(LearningSignalRow.created_at.desc())
        )
        return [row_to_signal(row) for row in rows.all()]

    async def record_outcome(self, outcome: OutcomeRecord) -> OutcomeRecord:
        self._session.add(outcome_to_row(outcome))
        await self._session.flush()
        return outcome

    async def outcomes_for(self, application_id: str) -> list[OutcomeRecord]:
        rows = await self._session.scalars(
            select(OutcomeRow).where(OutcomeRow.application_id == application_id)
        )
        return [row_to_outcome(row) for row in rows.all()]


__all__ = ["MemoryRepository"]
