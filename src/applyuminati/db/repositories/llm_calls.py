"""LLM call audit persistence."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.db.mappers import LLMCallLike, llm_call_to_row
from applyuminati.db.models import LLMCallRow


class LLMCallRepository:
    """Audit trail of model calls.

    Accepts the structural ``LLMCallLike`` protocol rather than importing
    :class:`applyuminati.llm.base.LLMCallRecord` — ``applyuminati.db`` sits
    below the ``llm`` layer and must not reach up into it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, call: LLMCallLike) -> None:
        self._session.add(llm_call_to_row(call))
        await self._session.flush()

    async def usage_summary(self, *, run_id: str | None = None) -> dict[str, float]:
        """Aggregate tokens and estimated cost, optionally scoped to a run."""
        statement = select(
            func.count().label("calls"),
            func.coalesce(func.sum(LLMCallRow.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(LLMCallRow.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(LLMCallRow.estimated_cost_usd), 0.0).label("cost"),
        )
        if run_id is not None:
            statement = statement.where(LLMCallRow.run_id == run_id)
        row = (await self._session.execute(statement)).one()
        return {
            "calls": float(row.calls),
            "input_tokens": float(row.input_tokens),
            "output_tokens": float(row.output_tokens),
            "estimated_cost_usd": float(row.cost),
        }

    async def failures(self, *, limit: int = 50) -> list[dict[str, str | None]]:
        """Recent failed calls, newest first, for the health UI."""
        rows = await self._session.scalars(
            select(LLMCallRow)
            .where(LLMCallRow.succeeded.is_(False))
            .order_by(LLMCallRow.started_at.desc())
            .limit(limit)
        )
        return [
            {
                "provider": row.provider,
                "model": row.model,
                "failure_category": row.failure_category,
                "failure_message": row.failure_message,
            }
            for row in rows.all()
        ]


__all__ = ["LLMCallRepository"]
