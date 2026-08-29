"""Usage tracking and budget enforcement."""

from __future__ import annotations

from typing import Any

from applyuminati.core.errors import ConfigurationError
from applyuminati.core.logging import get_logger
from applyuminati.core.settings import ProviderConfig
from applyuminati.llm.base import LLMCallRecord, TokenUsage

log = get_logger(__name__)

__all__ = ["UsageTracker"]


class UsageTracker:
    """Accumulates token usage and estimated cost, enforcing run budgets."""

    def __init__(self, *, run_budget_usd: float | None = None) -> None:
        self._run_budget_usd = run_budget_usd
        self._total_cost = 0.0
        self._by_provider: dict[str, dict[str, float]] = {}
        self._calls: list[LLMCallRecord] = []

    def check_budget(self, config: ProviderConfig) -> None:
        """Raise before a call that would exceed the run budget."""
        if self._run_budget_usd is not None and self._total_cost >= self._run_budget_usd:
            raise ConfigurationError(
                f"run budget ${self._run_budget_usd:.2f} would be exceeded "
                f"(current spend: ${self._total_cost:.4f})",
                code="configuration.budget_exceeded",
                details={"budget": self._run_budget_usd, "spent": self._total_cost},
            )

    def record(self, call: LLMCallRecord) -> None:
        """Record a completed call."""
        self._calls.append(call)
        if call.estimated_cost_usd:
            self._total_cost += call.estimated_cost_usd
        key = call.provider
        if key not in self._by_provider:
            self._by_provider[key] = {"calls": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0}
        bucket = self._by_provider[key]
        bucket["calls"] += 1
        bucket["cost"] += call.estimated_cost_usd or 0.0
        bucket["input_tokens"] += call.input_tokens or 0
        bucket["output_tokens"] += call.output_tokens or 0

    def totals(self) -> dict[str, float]:
        return {
            "calls": float(len(self._calls)),
            "estimated_cost_usd": self._total_cost,
        }

    def by_provider(self) -> dict[str, dict[str, float]]:
        return dict(self._by_provider)
