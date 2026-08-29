"""Anthropic Messages API adapter."""

from __future__ import annotations

import time
from typing import Any

import httpx

from applyuminati.core.errors import (
    AuthenticationRequiredError,
    RateLimitedError,
    TransientNetworkError,
)
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import ProviderConfig, Settings
from applyuminati.llm.base import (
    CompletionRequest,
    CompletionResponse,
    LLMCapability,
    LLMProvider,
    ModelT,
    ProviderMetadata,
    Role,
    TokenUsage,
    llm_plugin,
)
from applyuminati.llm.structured import request_structured

__all__ = ["PLUGIN", "AnthropicProvider"]

_DEFAULT_URL = "https://api.anthropic.com/v1"
_CAPABILITIES = frozenset(
    {LLMCapability.CHAT, LLMCapability.STRUCTURED_OUTPUT, LLMCapability.STREAMING}
)


class AnthropicProvider(LLMProvider):
    def __init__(self, settings: Settings, name: str, config: ProviderConfig) -> None:
        self._name = name
        self._config = config
        self._base_url = config.base_url or _DEFAULT_URL
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=config.timeout_seconds,
            headers={
                "x-api-key": config.api_key.get_secret_value() if config.api_key else "",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                **config.extra_headers,
            },
        )

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            slug=self._name,
            name=self._name,
            capabilities=_CAPABILITIES,
            remote=True,
            homepage="https://docs.anthropic.com",
        )

    async def health(self) -> HealthReport:
        if not self._config.api_key:
            return HealthReport(plugin=self._name, state=HealthState.DEGRADED, detail="no API key")
        try:
            # Anthropic has no models endpoint; a 1-token call is the cheapest probe.
            resp = await self._client.post(
                "/messages",
                json={
                    "model": self._config.default_model or "claude-3-5-haiku-20241022",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            if resp.status_code < 400:
                return HealthReport(
                    plugin=self._name, state=HealthState.HEALTHY, detail="reachable"
                )
            if resp.status_code in (401, 403):
                return HealthReport(
                    plugin=self._name, state=HealthState.UNAVAILABLE, detail="auth failed"
                )
        except Exception as exc:
            return HealthReport(plugin=self._name, state=HealthState.UNAVAILABLE, detail=str(exc))
        return HealthReport(
            plugin=self._name, state=HealthState.UNAVAILABLE, detail="health check failed"
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self._config.default_model or "claude-3-5-haiku-20241022"
        system = request.system_prompt or ""
        messages = [
            {"role": m.role.value if m.role is not Role.SYSTEM else "user", "content": m.content}
            for m in request.conversation
        ]
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens or 4096,
            "messages": messages,
            "temperature": request.temperature,
        }
        if system:
            body["system"] = system
        if request.response_schema:
            body["tools"] = [
                {
                    "name": "respond",
                    "description": "Respond with structured output",
                    "input_schema": request.response_schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": "respond"}

        start = time.perf_counter()
        try:
            resp = await self._client.post("/messages", json=body)
        except httpx.TimeoutException as exc:
            raise TransientNetworkError(f"timeout: {exc}", code="llm.timeout") from exc
        except httpx.ConnectError as exc:
            raise TransientNetworkError(
                f"connection failed: {exc}", code="llm.connect_error"
            ) from exc

        self._translate_error(resp)
        data = resp.json()
        latency_ms = (time.perf_counter() - start) * 1000
        usage_data = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=usage_data.get("input_tokens"),
            output_tokens=usage_data.get("output_tokens"),
        )
        # Extract text or tool_use output.
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                import json

                text = json.dumps(block.get("input", {}))
        return CompletionResponse(
            text=text,
            provider=self._name,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            estimated_cost_usd=usage.estimated_cost_usd(self._config),
            finish_reason=data.get("stop_reason"),
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
        )

    async def complete_structured(
        self, request: CompletionRequest, schema: type[ModelT]
    ) -> tuple[ModelT, CompletionResponse]:
        return await request_structured(self, request, schema)

    async def stream(self, request: CompletionRequest) -> Any:  # type: ignore[override]
        response = await self.complete(request)
        yield response.text

    async def aclose(self) -> None:
        await self._client.aclose()

    def _translate_error(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            seconds = float(retry_after) if retry_after else None
            raise RateLimitedError(
                "rate limited", code="llm.rate_limited", retry_after_seconds=seconds
            )
        if resp.status_code in (401, 403):
            raise AuthenticationRequiredError(
                f"auth failed (HTTP {resp.status_code})", code="llm.auth_required"
            )
        if resp.status_code >= 500:
            raise TransientNetworkError(
                f"server error (HTTP {resp.status_code})", code="llm.server_error"
            )


def _factory(settings: Settings, name: str, config: ProviderConfig) -> AnthropicProvider:
    return AnthropicProvider(settings, name, config)


PLUGIN = llm_plugin(
    slug="anthropic",
    name="Anthropic",
    factory=_factory,
    capabilities=_CAPABILITIES,
    description="Anthropic Messages API (Claude).",
    priority=9,
)
