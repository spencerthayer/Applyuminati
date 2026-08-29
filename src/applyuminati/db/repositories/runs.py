"""Run-record persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.models.task import RunRecord
from applyuminati.db.mappers import row_to_run, run_to_row
from applyuminati.db.models import RunRow


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, run: RunRecord) -> RunRecord:
        self._session.add(run_to_row(run))
        await self._session.flush()
        return run

    async def get(self, run_id: str) -> RunRecord | None:
        row = await self._session.get(RunRow, run_id)
        return row_to_run(row) if row else None

    async def list(self, *, kind: str | None = None, limit: int = 20) -> list[RunRecord]:
        statement = select(RunRow)
        if kind is not None:
            statement = statement.where(RunRow.kind == kind)
        statement = statement.order_by(RunRow.started_at.desc()).limit(limit)
        rows = (await self._session.scalars(statement)).all()
        return [row_to_run(row) for row in rows]

    async def save(self, run: RunRecord) -> RunRecord:
        row = await self._session.get(RunRow, run.id)
        if row is None:
            row = RunRow(id=run.id)
            self._session.add(row)
        run_to_row(run, row=row)
        await self._session.flush()
        return run


__all__ = ["RunRepository"]
