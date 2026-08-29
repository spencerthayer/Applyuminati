"""LLM client facade: one entry point for every model call.

Resolves a provider from settings, applies usage tracking, and provides
``structured()`` for schema-validated output through the prompt registry.

When no provider is configured, ``is_configured`` is False and every method
raises a clear error — callers check the flag and skip LLM paths, which is
how the product works deterministically with zero providers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from applyuminati.core.errors import BackendUnavailableError, ConfigurationError
from applyuminati.core.logging import get_logger
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import Settings
from applyuminati.llm.base import (
    CompletionRequest,
    CompletionResponse,
    LLMCallRecord,
    LLMCapability,
    LLMProvider,
    Message,
    ModelT,
)
from applyuminati.llm.prompts.base import PROMPT_REGISTRY, get_prompt
from applyuminati.llm.structured import request_structured
from applyuminati.llm.usage import UsageTracker

log = get_logger(__name__)

__all__ = ["LLMClient"]


class LLMClient:
    """One facade for every model call in the system."""

    def __init__(self, settings: Settings, *, call_sink: Callable[[LLMCallRecord], Awaitable[None]] | None = None) -> None:
        self._settings = settings
        self._providers: dict[str, LLMProvider] = {}
        self._tracker = UsageTracker(run_budget_usd=settings.llm.run_budget_usd)
        self._call_sink = call_sink

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> LLMClient:
        return cls(settings, **kwargs)

    @property
    def is_configured(self) -> bool:
        """True when at least one enabled provider exists and LLM is enabled."""
        if not self._settings.llm.enabled:
            return False
        return any(cfg.enabled for cfg in self._settings.llm.providers.values())

    def _get_provider(self, name: str | None = None) -> LLMProvider:
        if not self.is_configured:
            raise ConfigurationError(
                "no LLM provider is configured; set APPLYUMINATI_LLM__PROVIDERS__OPENAI__API_KEY "
                "or configure a provider in config.toml",
                code="configuration.no_llm_provider",
            )
        resolved_name = name or self._settings.llm.default_provider
        if resolved_name is None:
            # Pick the first enabled provider.
            for pname, cfg in self._settings.llm.providers.items():
                if cfg.enabled:
                    resolved_name = pname
                    break
        if resolved_name is None or resolved_name not in self._settings.llm.providers:
            raise ConfigurationError(
                f"no LLM provider resolved; configured: {sorted(self._settings.llm.providers)}",
                code="configuration.no_llm_provider",
            )
        config = self._settings.llm.providers[resolved_name]
        if not config.enabled:
            raise ConfigurationError(
                f"provider {resolved_name!r} is disabled",
                code="configuration.provider_disabled",
            )
        if resolved_name not in self._providers:
            from applyuminati.llm.base import LLM_REGISTRY

            descriptor = LLM_REGISTRY.get(config.kind)
            self._providers[resolved_name] = descriptor.create(
                settings=self._settings,
                name=resolved_name,
                config=config,
            )
        return self._providers[resolved_name]

    async def complete(
        self, request: CompletionRequest, *, provider: str | None = None
    ) -> CompletionResponse:
        impl = self._get_provider(provider)
        config = self._settings.llm.providers[provider or self._settings.llm.default_provider or ""]
        self._tracker.check_budget(config)
        import time

        start = time.perf_counter()
        call = LLMCallRecord(
            provider=provider or self._settings.llm.default_provider or "unknown",
            model=request.model or config.default_model or "unknown",
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            run_id=request.run_id,
            task_id=request.task_id,
        )
        try:
            response = await impl.complete(request)
            call.succeeded = True
            call.latency_ms = response.latency_ms
            call.input_tokens = response.usage.input_tokens
            call.output_tokens = response.usage.output_tokens
            call.estimated_cost_usd = response.estimated_cost_usd
            self._tracker.record(call)
            if self._call_sink:
                await self._call_sink(call)
            return response
        except Exception as exc:
            call.succeeded = False
            call.latency_ms = (time.perf_counter() - start) * 1000
            call.failure_message = str(exc)[:500]
            self._tracker.record(call)
            if self._call_sink:
                await self._call_sink(call)
            raise

    async def structured(
        self,
        prompt_id: str,
        schema: type[ModelT],
        *,
        provider: str | None = None,
        model: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        **render_kwargs: Any,
    ) -> tuple[ModelT, CompletionResponse]:
        prompt = get_prompt(prompt_id)
        messages = prompt.render(**render_kwargs)
        request = CompletionRequest(
            messages=messages,
            model=model,
            prompt_id=prompt.id,
            prompt_version=prompt.full_id,
            run_id=run_id,
            task_id=task_id,
        )
        impl = self._get_provider(provider)
        return await request_structured(impl, request, schema)

    async def health(self) -> list[HealthReport]:
        if not self.is_configured:
            return []

        async def probe(name: str, config: Any) -> HealthReport:  # noqa: ANN401
            try:
                impl = self._get_provider(name)
                return await impl.health()
            except Exception as exc:  # noqa: BLE001
                return HealthReport(plugin=name, state=HealthState.UNAVAILABLE, detail=str(exc))

        tasks = [
            probe(name, cfg)
            for name, cfg in self._settings.llm.providers.items()
            if cfg.enabled
        ]
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        reports: list[HealthReport] = []
        for result in results:
            if isinstance(result, HealthReport):
                reports.append(result)
            elif isinstance(result, Exception):
                reports.append(
                    HealthReport(plugin="unknown", state=HealthState.UNAVAILABLE, detail=str(result))
                )
        return reports

    async def aclose(self) -> None:
        for provider in self._providers.values():
            try:
                await provider.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._providers.clear()
