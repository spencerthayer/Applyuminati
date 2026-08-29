"""Fit-score persistence. Scores are append-only history, never overwritten."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.models.scoring import FitScore
from applyuminati.db.mappers import row_to_score, score_to_row
from applyuminati.db.models import FitScoreRow


class ScoreRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, score: FitScore) -> FitScore:
        self._session.add(score_to_row(score))
        await self._session.flush()
        return score

    async def latest_for(self, job_id: str, profile_id: str) -> FitScore | None:
        row = (
            await self._session.scalars(
                select(FitScoreRow)
                .where(FitScoreRow.job_id == job_id, FitScoreRow.profile_id == profile_id)
                .order_by(FitScoreRow.scored_at.desc())
                .limit(1)
            )
        ).first()
        return row_to_score(row) if row else None

    async def latest_map(self, job_ids: Sequence[str], profile_id: str) -> dict[str, FitScore]:
        """Newest score per job in one round trip.

        Portable "latest per group": join against a derived table of
        ``(job_id, max(scored_at))`` rather than window functions or
        correlated-self tricks, so SQLite and PostgreSQL run identical SQL.
        """
        if not job_ids:
            return {}
        inner = (
            select(FitScoreRow.job_id, func.max(FitScoreRow.scored_at).label("newest"))
            .where(
                FitScoreRow.profile_id == profile_id,
                FitScoreRow.job_id.in_(list(job_ids)),
            )
            .group_by(FitScoreRow.job_id)
            .subquery()
        )
        rows = await self._session.scalars(
            select(FitScoreRow).join(
                inner,
                and_(
                    FitScoreRow.job_id == inner.c.job_id,
                    FitScoreRow.scored_at == inner.c.newest,
                ),
            )
        )
        return {row.job_id: row_to_score(row) for row in rows.all()}

    async def counts_by_recommendation(self, profile_id: str) -> dict[str, int]:
        """Counts of the *latest* score's recommendation per job.

        Grouping by recommendation directly would double-count rescored jobs,
        so we collapse to one score per job first.
        """
        newest = (
            select(FitScoreRow)
            .where(FitScoreRow.profile_id == profile_id)
            .order_by(FitScoreRow.scored_at.desc())
        )
        seen: set[str] = set()
        counts: dict[str, int] = {}
        for row in (await self._session.scalars(newest)).all():
            if row.job_id in seen:
                continue
            seen.add(row.job_id)
            counts[row.recommendation] = counts.get(row.recommendation, 0) + 1
        return counts


__all__ = ["ScoreRepository"]
