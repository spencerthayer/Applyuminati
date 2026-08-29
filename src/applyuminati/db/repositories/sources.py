"""Per-source enablement and health state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.clock import utcnow
from applyuminati.core.registry import HealthState
from applyuminati.db.models import SourceStateRow


@dataclass(frozen=True, slots=True)
class SourceState:
    """Snapshot of one source's persisted state."""

    slug: str
    enabled: bool
    options: dict[str, object]
    health_state: HealthState
    health_detail: str
    last_checked_at: datetime | None
    last_run_at: datetime | None
    last_run_jobs: int
    consecutive_failures: int


def _to_state(row: SourceStateRow) -> SourceState:
    return SourceState(
        slug=row.slug,
        enabled=row.enabled,
        options=dict(row.options),
        health_state=HealthState(row.health_state),
        health_detail=row.health_detail,
        last_checked_at=row.last_checked_at,
        last_run_at=row.last_run_at,
        last_run_jobs=row.last_run_jobs,
        consecutive_failures=row.consecutive_failures,
    )


class SourceStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def all(self) -> dict[str, SourceState]:
        rows = await self._session.scalars(select(SourceStateRow))
        return {row.slug: _to_state(row) for row in rows.all()}

    async def get(self, slug: str) -> SourceState | None:
        row = await self._session.get(SourceStateRow, slug)
        return _to_state(row) if row else None

    async def set_enabled(
        self, slug: str, enabled: bool, options: dict[str, object] | None = None
    ) -> SourceState:
        row = await self._session.get(SourceStateRow, slug)
        if row is None:
            row = SourceStateRow(slug=slug)
            self._session.add(row)
        row.enabled = enabled
        if options is not None:
            row.options = dict(options)
        await self._session.flush()
        return _to_state(row)

    async def record_health(self, slug: str, state: HealthState, detail: str) -> None:
        row = await self._session.get(SourceStateRow, slug)
        if row is None:
            row = SourceStateRow(slug=slug)
            self._session.add(row)
        row.health_state = state.value
        row.health_detail = detail
        row.last_checked_at = utcnow()
        await self._session.flush()

    async def record_run(self, slug: str, jobs: int, failed: bool) -> None:
        row = await self._session.get(SourceStateRow, slug)
        if row is None:
            row = SourceStateRow(slug=slug)
            self._session.add(row)
        row.last_run_at = utcnow()
        row.last_run_jobs = jobs
        row.consecutive_failures = row.consecutive_failures + 1 if failed else 0
        await self._session.flush()


__all__ = ["SourceState", "SourceStateRepository"]
