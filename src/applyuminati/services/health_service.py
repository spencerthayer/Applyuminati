"""Health and availability.

Everything here answers one question honestly: *what actually works on this
machine right now?* Nothing is assumed available, and a probe that fails
becomes an ``UNAVAILABLE`` report with an actionable detail string rather than
an exception — the health page must never be the thing that is broken.

This is also what makes graceful degradation real: callers consult these
reports before choosing a backend, so a host without ego lite, without
Playwright browsers and without an API key still runs the deterministic
pipeline end to end.
"""

from __future__ import annotations

import asyncio
from typing import Any

from applyuminati.agents.base import AGENT_REGISTRY
from applyuminati.browser.base import BROWSER_REGISTRY
from applyuminati.core.logging import get_logger
from applyuminati.core.registry import HealthReport, HealthState, Registry
from applyuminati.core.settings import Settings
from applyuminati.email.base import EMAIL_REGISTRY
from applyuminati.llm.base import LLM_REGISTRY
from applyuminati.llm.client import LLMClient
from applyuminati.services.container import Repositories
from applyuminati.services.source_service import SourceService
from applyuminati.services.views import BackendHealthView
from applyuminati.sources.base import SOURCE_REGISTRY

log = get_logger(__name__)


async def _probe(name: str, factory: Any) -> HealthReport:
    """Instantiate and probe one backend, converting any failure to a report."""
    try:
        backend = factory()
        return await backend.health()
    except Exception as exc:
        return HealthReport(plugin=name, state=HealthState.UNAVAILABLE, detail=str(exc))


class HealthService:
    def __init__(
        self, repos: Repositories, settings: Settings, llm: LLMClient | None = None
    ) -> None:
        self._repos = repos
        self._settings = settings
        self._llm = llm

    async def summary(self, database_ok: bool, schema_version: str | None) -> dict[str, Any]:
        profile = await self._repos.profiles.get_active()
        states = await self._repos.sources.all()
        enabled = sorted(slug for slug, state in states.items() if state.enabled)
        return {
            "status": "ok" if database_ok else "degraded",
            "database_ok": database_ok,
            "schema_version": schema_version,
            "execution_mode": self._settings.execution_mode.value,
            "profile_configured": profile is not None,
            "enabled_sources": enabled,
        }

    async def backends(self) -> BackendHealthView:
        """Probe every registered backend concurrently."""
        source_service = SourceService(self._repos, self._settings)
        sources_task = self._probe_sources(source_service)
        llm_task = self._probe_llm()
        browsers_task = self._probe_simple(BROWSER_REGISTRY)
        agents_task = self._probe_simple(AGENT_REGISTRY)
        email_task = self._probe_email()

        sources, llm, browsers, agents, email = await asyncio.gather(
            sources_task, llm_task, browsers_task, agents_task, email_task
        )
        return BackendHealthView(
            sources=sources,
            llm=llm,
            browsers=browsers,
            agents=agents,
            email=email,
            load_errors=self._load_errors(),
        )

    def _load_errors(self) -> list[str]:
        errors: list[str] = []
        for registry in (
            SOURCE_REGISTRY,
            LLM_REGISTRY,
            BROWSER_REGISTRY,
            AGENT_REGISTRY,
            EMAIL_REGISTRY,
        ):
            errors.extend(
                f"{err.kind}:{err.slug} from {err.origin}: {err.message}"
                for err in registry.load_errors
            )
        return errors

    async def _probe_sources(self, service: SourceService) -> list[HealthReport]:
        views = await service.list(probe_health=True)
        return [
            view.health
            or HealthReport(plugin=view.slug, state=HealthState.UNKNOWN, detail="not probed")
            for view in views
        ]

    async def _probe_llm(self) -> list[HealthReport]:
        if self._llm is None:
            return []
        try:
            return await self._llm.health()
        except Exception as exc:
            log.warning("health.llm_probe_failed", error=str(exc))
            return [HealthReport(plugin="llm", state=HealthState.UNAVAILABLE, detail=str(exc))]

    async def _probe_simple(self, registry: Registry[Any]) -> list[HealthReport]:
        """Probe backends whose constructor takes only settings."""
        descriptors = registry.all()
        return list(
            await asyncio.gather(
                *(
                    _probe(d.slug, lambda d=d: d.create(settings=self._settings))
                    for d in descriptors
                )
            )
        )

    async def _probe_email(self) -> list[HealthReport]:
        reports: list[HealthReport] = []
        for name, account in self._settings.email.accounts.items():
            if not account.enabled:
                reports.append(
                    HealthReport(
                        plugin=name,
                        state=HealthState.UNKNOWN,
                        detail="account is configured but disabled",
                    )
                )
                continue
            descriptor = EMAIL_REGISTRY.try_get(account.kind)
            if descriptor is None:
                reports.append(
                    HealthReport(
                        plugin=name,
                        state=HealthState.UNAVAILABLE,
                        detail=f"unknown email provider kind {account.kind!r}",
                    )
                )
                continue
            reports.append(
                await _probe(
                    name, lambda d=descriptor, a=account, n=name: d.create(account=a, name=n)
                )
            )
        return reports


__all__ = ["HealthService"]
