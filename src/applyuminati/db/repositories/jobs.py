"""Job posting persistence: the deduplication write path.

The merge rule lives here because it is fundamentally a persistence concern:
two observations of one opening must become one row that keeps both
provenance records, with the most authoritative source winning the scalar
fields.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.models.job import Job, JobSourceRecord, PipelineStage, VerificationState
from applyuminati.db.mappers import (
    job_canonical_values,
    job_lifecycle_values,
    job_to_row,
    row_to_job,
    row_to_source_record,
    source_record_to_row,
)
from applyuminati.db.models import ApplicationRow, FitScoreRow, JobRow, JobSourceRow


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: str) -> Job | None:
        row = await self._session.get(JobRow, job_id)
        return self._hydrate(row) if row else None

    async def get_by_identity(self, identity_key: str) -> Job | None:
        row = (
            await self._session.scalars(
                select(JobRow).where(JobRow.identity_key == identity_key).limit(1)
            )
        ).first()
        return self._hydrate(row) if row else None

    async def find_by_source(self, source: str, source_job_id: str) -> Job | None:
        row = (
            await self._session.scalars(
                select(JobRow)
                .join(JobSourceRow)
                .where(
                    JobSourceRow.source == source,
                    JobSourceRow.source_job_id == source_job_id,
                )
            )
        ).first()
        return self._hydrate(row) if row else None

    async def find_by_canonical_url(self, url: str) -> Job | None:
        row = (
            await self._session.scalars(
                select(JobRow)
                .join(JobSourceRow)
                .where(JobSourceRow.canonical_url == url)
                .limit(1)
            )
        ).first()
        return self._hydrate(row) if row else None

    async def upsert(self, job: Job) -> tuple[Job, bool]:
        """Insert, or merge into the existing row with this identity key.

        Merge adds source records not seen before, refreshes lifecycle fields,
        and lets the highest-tier observation win the scalar fields — a direct
        ATS record corrects an aggregator's copy, never the reverse.
        """
        row = (
            await self._session.scalars(
                select(JobRow).where(JobRow.identity_key == job.identity_key).limit(1)
            )
        ).first()
        if row is None:
            row = job_to_row(job)
            self._session.add(row)
            await self._session.flush()
            return job, True

        existing_records = [row_to_source_record(record) for record in row.sources]
        incoming_best = max(job.sources, key=lambda record: record.priority, default=None)
        existing_best = max(existing_records, key=lambda record: record.priority, default=None)
        if incoming_best is not None and (
            existing_best is None or incoming_best.priority > existing_best.priority
        ):
            for key, value in job_canonical_values(job).items():
                setattr(row, key, value)

        known = {(record.source, record.source_job_id) for record in existing_records}
        for record in job.sources:
            if (record.source, record.source_job_id) in known:
                continue
            self._session.add(source_record_to_row(record, row.id))

        for key, value in job_lifecycle_values(job).items():
            setattr(row, key, value)
        row.last_seen_at = max(row.last_seen_at, job.last_seen_at)
        await self._session.flush()
        return self._hydrate(row), False

    async def add_source_record(self, job_id: str, record: JobSourceRecord) -> None:
        row = await self._session.get(JobRow, job_id)
        if row is None:
            msg = f"job {job_id} does not exist"
            raise ValueError(msg)
        self._session.add(source_record_to_row(record, row.id))
        await self._session.flush()

    async def list(
        self,
        *,
        query: str | None = None,
        sources: list[str] | None = None,
        recommendation: str | None = None,
        min_score: float | None = None,
        states: list[str] | None = None,
        companies: list[str] | None = None,
        remote_modes: list[str] | None = None,
        verification: str | None = None,
        has_score: bool | None = None,
        sort: str = "discovered_at",
        descending: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Job], int]:
        statement = select(JobRow)
        if query:
            pattern = f"%{query}%"
            statement = statement.where(
                or_(JobRow.title.ilike(pattern), JobRow.company.ilike(pattern))
            )
        if sources:
            statement = statement.join(JobSourceRow).where(JobSourceRow.source.in_(sources))
        if companies:
            statement = statement.where(JobRow.company_key.in_([c.lower() for c in companies]))
        if remote_modes:
            statement = statement.where(JobRow.remote_mode.in_(remote_modes))
        if verification:
            statement = statement.where(JobRow.verification == verification)
        if states:
            statement = statement.where(JobRow.id.in_(select(ApplicationRow.job_id)))

        # Score-driven filters join the newest score per job; the correlated
        # max(scored_at) is portable across SQLite and PostgreSQL.
        if recommendation is not None or min_score is not None:
            latest = select(func.max(FitScoreRow.scored_at)).where(FitScoreRow.job_id == JobRow.id)
            statement = (
                statement.join(FitScoreRow)
                .where(FitScoreRow.scored_at == latest)
            )
            if recommendation is not None:
                statement = statement.where(FitScoreRow.recommendation == recommendation)
            if min_score is not None:
                statement = statement.where(FitScoreRow.overall >= min_score)

        total = await self._session.scalar(
            select(func.count()).select_from(statement.subquery())
        ) or 0

        if has_score is not None:
            scored_ids = select(FitScoreRow.job_id).distinct()
            statement = (
                statement.where(JobRow.id.in_(scored_ids))
                if has_score
                else statement.where(JobRow.id.not_in(scored_ids))
            )

        sort_column = getattr(JobRow, sort, JobRow.discovered_at)
        statement = statement.order_by(sort_column.desc() if descending else sort_column.asc())
        statement = statement.limit(limit).offset(offset)

        rows = (await self._session.scalars(statement)).unique().all()
        return [self._hydrate(row) for row in rows], int(total)

    async def count_by_source(self) -> dict[str, int]:
        rows = await self._session.execute(
            select(JobSourceRow.source, func.count(func.distinct(JobSourceRow.job_id))).group_by(
                JobSourceRow.source
            )
        )
        return {str(source): int(count) for source, count in rows.all()}

    async def set_stage(self, job_id: str, stage: PipelineStage) -> None:
        row = await self._session.get(JobRow, job_id)
        if row is not None:
            row.stage = stage.value
            await self._session.flush()

    async def record_verification(
        self, job_id: str, state: VerificationState, at: datetime
    ) -> None:
        row = await self._session.get(JobRow, job_id)
        if row is not None:
            row.verification = state.value
            row.last_verified_at = at
            await self._session.flush()

    def _hydrate(self, row: JobRow) -> Job:
        return row_to_job(row, row.sources)


__all__ = ["JobRepository"]
