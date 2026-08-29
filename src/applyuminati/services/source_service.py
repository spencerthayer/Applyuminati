"""Source enablement and health.

Joins three things the UI needs in one place: what plugins are registered,
which the user enabled, and whether each actually works right now. Health is
probed concurrently and failures become ``UNAVAILABLE`` reports rather than
exceptions, so one dead board never blanks the settings page.
"""

from __future__ import annotations

import asyncio
from typing import Any

from applyuminati.core.errors import ConfigurationError
from applyuminati.core.logging import get_logger
from applyuminati.core.registry import HealthReport, HealthState, PluginDescriptor
from applyuminati.core.settings import Settings
from applyuminati.services.container import Repositories
from applyuminati.services.views import SourceView
from applyuminati.sources.base import SOURCE_REGISTRY, JobSource

log = get_logger(__name__)


def _instantiate(
    descriptor: PluginDescriptor[JobSource], settings: Settings, options: dict[str, Any]
) -> JobSource:
    """Build a source instance, validating its options first."""
    descriptor.validate_options(options)
    return descriptor.create(settings=settings, options=options)


class SourceService:
    def __init__(self, repos: Repositories, settings: Settings) -> None:
        self._repos = repos
        self._settings = settings

    async def list(self, *, probe_health: bool = False) -> list[SourceView]:
        """Every registered source, with persisted state and optional health."""
        states = await self._repos.sources.all()
        descriptors = SOURCE_REGISTRY.all()

        health_by_slug: dict[str, HealthReport] = {}
        if probe_health:
            health_by_slug = await self._probe(descriptors, states)

        views: list[SourceView] = []
        for descriptor in descriptors:
            state = states.get(descriptor.slug)
            options = dict(state.options) if state else {}
            metadata = self._metadata_for(descriptor, options)
            views.append(
                SourceView(
                    slug=descriptor.slug,
                    name=descriptor.name,
                    description=descriptor.description,
                    tier=metadata["tier"],
                    ats=metadata["ats"],
                    enabled=bool(state and state.enabled),
                    capabilities=sorted(descriptor.capabilities),
                    requires_auth=descriptor.requires_auth,
                    blocking=metadata["blocking"],
                    options=options,
                    options_schema=(
                        descriptor.options_schema.model_json_schema()
                        if descriptor.options_schema
                        else None
                    ),
                    health=health_by_slug.get(descriptor.slug),
                    last_run_at=state.last_run_at if state else None,
                    last_run_jobs=state.last_run_jobs if state else 0,
                    consecutive_failures=state.consecutive_failures if state else 0,
                )
            )
        return views

    def _metadata_for(
        self, descriptor: PluginDescriptor[JobSource], options: dict[str, Any]
    ) -> dict[str, str]:
        """Read static metadata off an instance.

        Instantiation is cheap by contract (no network in ``__init__``), and
        reading metadata from the instance keeps a single source of truth
        rather than duplicating tier/ats onto the descriptor.
        """
        try:
            source = _instantiate(descriptor, self._settings, options)
        except ConfigurationError:
            return {"tier": "derived", "ats": "unknown", "blocking": "none"}
        meta = source.metadata
        return {
            "tier": meta.tier.value,
            "ats": meta.ats.value,
            "blocking": meta.blocking.value,
        }

    async def _probe(
        self, descriptors: list[PluginDescriptor[JobSource]], states: dict[str, Any]
    ) -> dict[str, HealthReport]:
        async def probe(descriptor: PluginDescriptor[JobSource]) -> tuple[str, HealthReport]:
            state = states.get(descriptor.slug)
            options = dict(state.options) if state else {}
            try:
                source = _instantiate(descriptor, self._settings, options)
                report = await source.health()
            except Exception as exc:
                report = HealthReport(
                    plugin=descriptor.slug,
                    state=HealthState.UNAVAILABLE,
                    detail=str(exc),
                )
            return descriptor.slug, report

        results = await asyncio.gather(*(probe(d) for d in descriptors))
        for slug, report in results:
            await self._repos.sources.record_health(slug, report.state, report.detail)
        return dict(results)

    async def set_enabled(
        self, slug: str, enabled: bool, *, options: dict[str, Any] | None = None
    ) -> SourceView:
        descriptor = SOURCE_REGISTRY.get(slug)
        merged = options
        if merged is not None:
            descriptor.validate_options(merged)
        await self._repos.sources.set_enabled(slug, enabled, merged)
        log.info("source.toggled", source=slug, enabled=enabled)
        views = await self.list()
        return next(view for view in views if view.slug == slug)

    async def enabled_sources(self) -> list[tuple[PluginDescriptor[JobSource], dict[str, Any]]]:
        """Descriptors and options for every enabled source."""
        states = await self._repos.sources.all()
        result: list[tuple[PluginDescriptor[JobSource], dict[str, Any]]] = []
        for slug, state in states.items():
            if not state.enabled:
                continue
            descriptor = SOURCE_REGISTRY.try_get(slug)
            if descriptor is None:
                log.warning("source.enabled_but_unregistered", source=slug)
                continue
            result.append((descriptor, dict(state.options)))
        return result

    async def sync_from_settings(self) -> int:
        """Apply ``settings.discovery.sources`` to the database.

        Config files are how a Docker deployment configures sources, so on
        startup the file is authoritative for any source it mentions; sources
        it does not mention keep their database state.
        """
        changed = 0
        for slug, config in self._settings.discovery.sources.items():
            if SOURCE_REGISTRY.try_get(slug) is None:
                log.warning("source.configured_but_unregistered", source=slug)
                continue
            state = await self._repos.sources.get(slug)
            if state is None or state.enabled != config.enabled or state.options != config.options:
                await self._repos.sources.set_enabled(slug, config.enabled, config.options)
                changed += 1
        return changed

    def instantiate(self, slug: str, options: dict[str, Any]) -> JobSource:
        return _instantiate(SOURCE_REGISTRY.get(slug), self._settings, options)


__all__ = ["SourceService"]
