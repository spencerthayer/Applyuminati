"""Google Gemini adapter."""

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

__all__ = ["PLUGIN", "GeminiProvider"]

_DEFAULT_URL = "https://generativelanguage.googleapis.com/v1beta"
_CAPABILITIES = frozenset(
    {
        LLMCapability.CHAT,
        LLMCapability.STRUCTURED_OUTPUT,
        LLMCapability.STREAMING,
        LLMCapability.VISION,
    }
)


class GeminiProvider(LLMProvider):
    def __init__(self, settings: Settings, name: str, config: ProviderConfig) -> None:
        self._name = name
        self._config = config
        self._base_url = config.base_url or _DEFAULT_URL
        self._api_key = config.api_key.get_secret_value() if config.api_key else ""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=config.timeout_seconds,
            headers={"content-type": "application/json", **config.extra_headers},
        )

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            slug=self._name,
            name=self._name,
            capabilities=_CAPABILITIES,
            remote=True,
            homepage="https://ai.google.dev",
        )

    async def health(self) -> HealthReport:
        if not self._api_key:
            return HealthReport(plugin=self._name, state=HealthState.DEGRADED, detail="no API key")
        try:
            resp = await self._client.get("/models", params={"key": self._api_key})
            if resp.status_code < 400:
                return HealthReport(
                    plugin=self._name, state=HealthState.HEALTHY, detail="reachable"
                )
        except Exception as exc:
            return HealthReport(plugin=self._name, state=HealthState.UNAVAILABLE, detail=str(exc))
        return HealthReport(
            plugin=self._name, state=HealthState.UNAVAILABLE, detail="health check failed"
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self._config.default_model or "gemini-1.5-flash"
        system = request.system_prompt
        contents = [
            {
                "role": "user" if m.role is not Role.ASSISTANT else "model",
                "parts": [{"text": m.content}],
            }
            for m in request.conversation
        ]
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": request.temperature},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if request.max_output_tokens:
            body["generationConfig"]["maxOutputTokens"] = request.max_output_tokens
        if request.response_schema:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseSchema"] = request.response_schema

        start = time.perf_counter()
        try:
            resp = await self._client.post(
                f"/models/{model}:generateContent",
                params={"key": self._api_key},
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise TransientNetworkError(f"timeout: {exc}", code="llm.timeout") from exc
        except httpx.ConnectError as exc:
            raise TransientNetworkError(
                f"connection failed: {exc}", code="llm.connect_error"
            ) from exc

        self._translate_error(resp)
        data = resp.json()
        latency_ms = (time.perf_counter() - start) * 1000
        usage_data = data.get("usageMetadata", {})
        usage = TokenUsage(
            input_tokens=usage_data.get("promptTokenCount"),
            output_tokens=usage_data.get("candidatesTokenCount"),
        )
        text = ""
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                text += part.get("text", "")
        return CompletionResponse(
            text=text,
            provider=self._name,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            estimated_cost_usd=usage.estimated_cost_usd(self._config),
            finish_reason=data.get("candidates", [{}])[0].get("finishReason")
            if data.get("candidates")
            else None,
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


def _factory(settings: Settings, name: str, config: ProviderConfig) -> GeminiProvider:
    return GeminiProvider(settings, name, config)


PLUGIN = llm_plugin(
    slug="gemini",
    name="Google Gemini",
    factory=_factory,
    capabilities=_CAPABILITIES,
    description="Google Gemini via the Generative Language API.",
    priority=8,
)
