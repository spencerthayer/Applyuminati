"""Job-source HTTP client with rate limiting and error translation.

One client per source, honouring the source's :class:`RateLimit` plus a global
politeness floor. Every transport and status failure maps to an
:class:`ApplyuminatiError` subclass, so a plugin's ``discover`` can capture
the failure into a :class:`SourceFailure` rather than raising.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from applyuminati.core.errors import (
    AuthenticationRequiredError,
    AutomationBlockedError,
    EndpointUnavailableError,
    RateLimitedError,
    TransientNetworkError,
)
from applyuminati.core.settings import Settings
from applyuminati.sources.base import RateLimit, SourceMetadata

__all__ = ["SourceHttpClient"]

#: Body signatures that indicate a bot interstitial rather than the real page.
_BOT_BLOCK_MARKERS = (
    "access denied",
    "are you a robot",
    "captcha",
    "cloudflare",
    "ddos protection",
    "please verify you are human",
    "unusual traffic",
)


class SourceHttpClient:
    """A rate-limited ``httpx`` wrapper tailored to one source."""

    def __init__(self, metadata: SourceMetadata, settings: Settings) -> None:
        self._metadata = metadata
        self._settings = settings
        self._min_interval = max(
            metadata.rate_limit.min_interval_seconds,
            settings.discovery.min_request_interval_seconds,
        )
        self._last_request_at = 0.0
        self._client = httpx.AsyncClient(
            timeout=settings.discovery.request_timeout_seconds,
            headers={"User-Agent": settings.discovery.user_agent},
            follow_redirects=True,
        )

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        wait = self._min_interval - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    def _translate(self, response: httpx.Response) -> None:
        """Raise the right error for a non-2xx response, or return silently."""
        if response.status_code < 400:
            return
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            seconds: float | None = None
            if retry_after:
                try:
                    seconds = float(retry_after)
                except ValueError:
                    seconds = None
            raise RateLimitedError(
                "rate limited by source",
                code="source.rate_limited",
                retry_after_seconds=seconds or self._metadata.rate_limit.backoff_seconds,
            )
        if response.status_code in (401, 403):
            body = response.text.lower()
            if response.status_code == 403 and any(marker in body for marker in _BOT_BLOCK_MARKERS):
                raise AutomationBlockedError(
                    "source served a bot interstitial",
                    code="source.automation_blocked",
                    details={"status": 403},
                )
            raise AuthenticationRequiredError(
                "source rejected credentials",
                code="source.auth_required",
                details={"status": response.status_code},
            )
        if response.status_code in (404, 410):
            raise EndpointUnavailableError(
                "source endpoint is gone",
                code="source.endpoint_gone",
                details={"status": response.status_code, "url": str(response.url)},
            )
        if response.status_code >= 500:
            raise TransientNetworkError(
                f"source returned {response.status_code}",
                code="source.server_error",
                details={"status": response.status_code},
            )
        # Everything else is still a problem, but not one we can classify finely.
        raise TransientNetworkError(
            f"unexpected status {response.status_code}",
            code="source.unexpected_status",
            details={"status": response.status_code},
        )

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
        await self._throttle()
        try:
            response = await self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise TransientNetworkError(
                f"request timed out: {exc}",
                code="source.timeout",
            ) from exc
        except httpx.TransportError as exc:
            raise TransientNetworkError(
                f"transport error: {exc}",
                code="source.transport_error",
            ) from exc
        self._translate(response)
        try:
            return response.json()
        except ValueError as exc:
            raise EndpointUnavailableError(
                f"expected JSON from {url} but could not parse: {exc}",
                code="source.bad_json",
            ) from exc

    async def get_text(self, url: str, *, params: dict[str, Any] | None = None) -> str:
        await self._throttle()
        try:
            response = await self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise TransientNetworkError(f"request timed out: {exc}", code="source.timeout") from exc
        except httpx.TransportError as exc:
            raise TransientNetworkError(f"transport error: {exc}", code="source.transport_error") from exc
        self._translate(response)
        return response.text

    async def head_status(self, url: str) -> int:
        await self._throttle()
        try:
            response = await self._client.head(url)
        except httpx.TimeoutException as exc:
            raise TransientNetworkError(f"request timed out: {exc}", code="source.timeout") from exc
        except httpx.TransportError as exc:
            raise TransientNetworkError(f"transport error: {exc}", code="source.transport_error") from exc
        # HEAD may return 405; follow up with a GET to learn the real status.
        if response.status_code == 405:
            return await self._get_status(url)
        return response.status_code

    async def _get_status(self, url: str) -> int:
        try:
            response = await self._client.get(url)
        except httpx.TransportError as exc:
            raise TransientNetworkError(f"transport error: {exc}", code="source.transport_error") from exc
        return response.status_code

    async def aclose(self) -> None:
        await self._client.aclose()
