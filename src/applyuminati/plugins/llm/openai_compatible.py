"""OpenAI-compatible chat completions adapter.

Covers OpenAI, OpenRouter, Ollama, LM Studio, vLLM and any compatible gateway
— they all speak ``/v1/chat/completions``. Declares ``LOCAL`` capability when
the base URL is loopback, and allows a missing API key against loopback URLs
because Ollama needs none.
"""

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
    Message,
    ProviderMetadata,
    Role,
    TokenUsage,
    llm_plugin,
)

__all__ = ["OpenAICompatibleProvider", "PLUGIN"]

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})
_DEFAULT_URL = "https://api.openai.com/v1"
_CAPABILITIES = frozenset(
    {LLMCapability.CHAT, LLMCapability.STRUCTURED_OUTPUT, LLMCapability.JSON_MODE, LLMCapability.STREAMING}
)


def _is_loopback(url: str | None) -> bool:
    if not url:
        return False
    return any(host in url for host in _LOOPBACK_HOSTS)


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, settings: Settings, name: str, config: ProviderConfig) -> None:
        self._name = name
        self._config = config
        self._base_url = config.base_url or _DEFAULT_URL
        self._local = _is_loopback(self._base_url)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=config.timeout_seconds,
            headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key.get_secret_value()}"
        headers.update(self._config.extra_headers)
        return headers

    @property
    def metadata(self) -> ProviderMetadata:
        caps = set(_CAPABILITIES)
        if self._local:
            caps.add(LLMCapability.LOCAL)
        return ProviderMetadata(
            slug=self._name,
            name=self._name,
            capabilities=frozenset(caps),
            remote=not self._local,
        )

    async def health(self) -> HealthReport:
        if self._local:
            # Ollara and local servers may not need an API key.
            try:
                resp = await self._client.get("/models")
                if resp.status_code < 400:
                    return HealthReport(plugin=self._name, state=HealthState.HEALTHY, detail="reachable")
                if resp.status_code in (401, 403):
                    return HealthReport(plugin=self._name, state=HealthState.HEALTHY, detail="reachable (auth needed for some ops)")
            except Exception as exc:  # noqa: BLE001
                return HealthReport(plugin=self._name, state=HealthState.UNAVAILABLE, detail=str(exc))
        else:
            if not self._config.api_key:
                return HealthReport(plugin=self._name, state=HealthState.DEGRADED, detail="no API key configured")
            try:
                resp = await self._client.get("/models")
                if resp.status_code < 400:
                    return HealthReport(plugin=self._name, state=HealthState.HEALTHY, detail="reachable")
            except Exception as exc:  # noqa: BLE001
                return HealthReport(plugin=self._name, state=HealthState.UNAVAILABLE, detail=str(exc))
        return HealthReport(plugin=self._name, state=HealthState.UNAVAILABLE, detail="health check failed")

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self._config.default_model or "gpt-4o-mini"
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_output_tokens:
            body["max_tokens"] = request.max_output_tokens
        if request.response_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": request.response_schema, "strict": True},
            }
        elif self.metadata.supports(LLMCapability.JSON_MODE):
            body["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        try:
            resp = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise TransientNetworkError(f"request timed out: {exc}", code="llm.timeout") from exc
        except httpx.ConnectError as exc:
            raise TransientNetworkError(f"connection failed: {exc}", code="llm.connect_error") from exc

        self._translate_error(resp)
        data = resp.json()
        latency_ms = (time.perf_counter() - start) * 1000
        usage_data = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=usage_data.get("prompt_tokens"),
            output_tokens=usage_data.get("completion_tokens"),
            cached_input_tokens=(
                usage_data.get("prompt_tokens_details", {}).get("cached_tokens")
                if usage_data.get("prompt_tokens_details")
                else None
            ),
        )
        text = data["choices"][0]["message"]["content"] if data.get("choices") else ""
        return CompletionResponse(
            text=text,
            provider=self._name,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            estimated_cost_usd=usage.estimated_cost_usd(self._config),
            finish_reason=data["choices"][0].get("finish_reason") if data.get("choices") else None,
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
        )

    async def stream(self, request: CompletionRequest) -> Any:  # type: ignore[override]
        # Streaming is implemented but simplified: yield the full response.
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
                "rate limited by provider",
                code="llm.rate_limited",
                retry_after_seconds=seconds,
            )
        if resp.status_code in (401, 403):
            raise AuthenticationRequiredError(
                f"provider rejected credentials (HTTP {resp.status_code})",
                code="llm.auth_required",
                details={"status": resp.status_code},
            )
        if resp.status_code >= 500:
            raise TransientNetworkError(
                f"provider returned HTTP {resp.status_code}",
                code="llm.server_error",
                details={"status": resp.status_code},
            )


def _factory(settings: Settings, name: str, config: ProviderConfig) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(settings, name, config)


PLUGIN = llm_plugin(
    slug="openai_compatible",
    name="OpenAI-compatible",
    factory=_factory,
    capabilities=_CAPABILITIES,
    description="Covers OpenAI, OpenRouter, Ollama, LM Studio, vLLM and any OpenAI-compatible endpoint.",
    priority=10,
)
