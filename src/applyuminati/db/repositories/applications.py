"""Application persistence: the event log is the record, the column is a cache."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.models.application import Application, ApplicationEvent
from applyuminati.db.mappers import (
    application_to_row,
    event_to_row,
    row_to_application,
)
from applyuminati.db.models import ApplicationRow


class ApplicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, application_id: str) -> Application | None:
        row = await self._session.get(ApplicationRow, application_id)
        return self._hydrate(row) if row else None

    async def get_for_job(self, job_id: str, profile_id: str) -> Application | None:
        row = (
            await self._session.scalars(
                select(ApplicationRow)
                .where(ApplicationRow.job_id == job_id, ApplicationRow.profile_id == profile_id)
                .limit(1)
            )
        ).first()
        return self._hydrate(row) if row else None

    async def ensure(self, job_id: str, profile_id: str) -> Application:
        """Return the existing application, or create one in DISCOVERED."""
        existing = await self.get_for_job(job_id, profile_id)
        if existing is not None:
            return existing
        application = Application(job_id=job_id, profile_id=profile_id)
        return await self.save(application)

    async def list(
        self,
        *,
        states: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Application], int]:
        from sqlalchemy import func

        statement = select(ApplicationRow)
        if states:
            statement = statement.where(ApplicationRow.state.in_(states))
        total = (
            await self._session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        )
        statement = statement.order_by(ApplicationRow.updated_at.desc()).limit(limit).offset(offset)
        rows = (await self._session.scalars(statement)).all()
        return [self._hydrate(row) for row in rows], int(total)

    async def save(self, application: Application) -> Application:
        row = await self._session.get(ApplicationRow, application.id)
        if row is None:
            row = ApplicationRow(id=application.id)
            self._session.add(row)
        application_to_row(application, row=row)
        await self._session.flush()
        return application

    async def append_event(self, event: ApplicationEvent) -> ApplicationEvent:
        self._session.add(event_to_row(event))
        await self._session.flush()
        return event

    async def counts_by_state(self, profile_id: str | None = None) -> dict[str, int]:
        from sqlalchemy import func

        statement = select(ApplicationRow.state, func.count()).group_by(ApplicationRow.state)
        if profile_id is not None:
            statement = statement.where(ApplicationRow.profile_id == profile_id)
        rows = await self._session.execute(statement)
        return {str(state): int(count) for state, count in rows.all()}

    def _hydrate(self, row: ApplicationRow) -> Application:
        return row_to_application(row, row.events, row.artifacts)


__all__ = ["ApplicationRepository"]
