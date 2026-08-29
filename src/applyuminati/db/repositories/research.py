"""Company research cache persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.models.research import CompanyResearch
from applyuminati.db.mappers import research_to_row, row_to_research
from applyuminati.db.models import CompanyResearchRow


class ResearchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, company_key: str) -> CompanyResearch | None:
        row = (
            await self._session.scalars(
                select(CompanyResearchRow)
                .where(CompanyResearchRow.company_key == company_key)
                .limit(1)
            )
        ).first()
        return row_to_research(row) if row else None

    async def upsert(self, research: CompanyResearch) -> CompanyResearch:
        row = (
            await self._session.scalars(
                select(CompanyResearchRow)
                .where(CompanyResearchRow.company_key == research.company_key)
                .limit(1)
            )
        ).first()
        if row is None:
            row = CompanyResearchRow(id=research.id)
            self._session.add(row)
        research_to_row(research, row=row)
        await self._session.flush()
        return research

    async def list(self, *, limit: int = 100) -> list[CompanyResearch]:
        rows = await self._session.scalars(
            select(CompanyResearchRow).order_by(CompanyResearchRow.updated_at.desc()).limit(limit)
        )
        return [row_to_research(row) for row in rows.all()]


__all__ = ["ResearchRepository"]
