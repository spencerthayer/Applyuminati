"""Discovery: fetch, normalise, deduplicate, persist.

This is the vertical slice's spine. Design points that matter:

* **Sources run concurrently and independently.** One board being down, rate
  limited or drifted must not reduce what the others contribute.
* **Failures are recorded, not raised.** Every :class:`SourceFailure` lands in
  the run record and in source memory, and the run finishes as ``PARTIAL``
  rather than pretending nothing happened.
* **Deduplication happens on write.** :meth:`JobRepository.upsert` merges by
  identity key and keeps every source record, so a job seen through three
  channels is one row with three provenance rows — never three jobs and never
  a discarded observation.
"""

from __future__ import annotations

import asyncio
import time

from applyuminati.core.errors import ApplyuminatiError, FailureCategory
from applyuminati.core.logging import bound_context, get_logger
from applyuminati.core.models.job import Job, PipelineStage
from applyuminati.core.models.memory import MemoryKind
from applyuminati.core.models.task import RunRecord, RunState
from applyuminati.core.provenance import AssertionLevel, Provenance, ProvenanceKind
from applyuminati.core.settings import Settings
from applyuminati.core.strategy import SearchStrategy
from applyuminati.memory.store import MemoryStore
from applyuminati.services.container import Repositories
from applyuminati.services.profile_service import ProfileService
from applyuminati.services.source_service import SourceService
from applyuminati.sources.base import DiscoveryRequest, SourceFailure, SourceResult

log = get_logger(__name__)


class DiscoveryService:
    def __init__(self, repos: Repositories, settings: Settings) -> None:
        self._repos = repos
        self._settings = settings
        self._sources = SourceService(repos, settings)
        self._profiles = ProfileService(repos)
        self._memory = MemoryStore(repos.memory)

    async def discover(
        self,
        *,
        sources: list[str] | None = None,
        queries: list[str] | None = None,
        locations: list[str] | None = None,
        triggered_by: str = "api",
    ) -> RunRecord:
        """Run discovery across the selected sources and persist the results."""
        profile = await self._profiles.try_get()
        strategy = profile.strategy if profile else SearchStrategy()
        resolved_queries = await self._profiles.search_queries(queries)
        resolved_locations = locations or (
            [loc.display() for loc in profile.targets.locations] if profile else []
        )

        selected = await self._select_sources(sources)
        run = RunRecord(
            kind="discovery",
            triggered_by=triggered_by,
            parameters={
                "sources": [slug for slug, _ in selected],
                "queries": resolved_queries,
                "locations": resolved_locations,
            },
        )
        run = await self._repos.runs.create(run)

        if not selected:
            run.state = RunState.FAILED
            run.failures.append(
                "no sources enabled; enable one with `applyuminati sources enable <slug>`"
            )
            return await self._finish(run)

        with bound_context(run_id=run.id, kind="discovery"):
            results = await self._fetch_all(
                selected,
                strategy=strategy,
                queries=resolved_queries,
                locations=resolved_locations,
                run_id=run.id,
            )
            await self._persist(run, results)

        return await self._finish(run)

    # -- internals --------------------------------------------------------

    async def _select_sources(self, requested: list[str] | None) -> list[tuple[str, dict]]:
        enabled = await self._sources.enabled_sources()
        pairs = [(descriptor.slug, options) for descriptor, options in enabled]
        if requested:
            wanted = set(requested)
            pairs = [pair for pair in pairs if pair[0] in wanted]
        return pairs

    async def _fetch_all(
        self,
        selected: list[tuple[str, dict]],
        *,
        strategy: SearchStrategy,
        queries: list[str],
        locations: list[str],
        run_id: str,
    ) -> list[SourceResult]:
        async def fetch(slug: str, options: dict) -> SourceResult:
            request = DiscoveryRequest.from_strategy(
                strategy,
                queries=queries,
                locations=locations,
                options=options,
                run_id=run_id,
            )
            started = time.perf_counter()
            with bound_context(source=slug, stage="discover"):
                try:
                    source = self._sources.instantiate(slug, options)
                    result = await source.discover(request)
                except ApplyuminatiError as exc:
                    # A plugin that raises anyway is a plugin bug, but the run
                    # must still complete: convert and carry on.
                    log.warning("discovery.source_raised", source=slug, error=exc.code)
                    result = SourceResult(
                        source=slug,
                        failures=[SourceFailure.from_error(slug, exc, stage="discover")],
                    )
                except Exception as exc:
                    log.exception("discovery.source_crashed", source=slug)
                    result = SourceResult(
                        source=slug,
                        failures=[
                            SourceFailure(
                                source=slug,
                                category=FailureCategory.UNKNOWN,
                                message=f"{type(exc).__name__}: {exc}",
                                stage="discover",
                            )
                        ],
                    )
            result.duration_seconds = time.perf_counter() - started
            log.info(
                "discovery.source_finished",
                source=slug,
                jobs=len(result.jobs),
                failures=len(result.failures),
                duration_s=round(result.duration_seconds, 2),
            )
            return result

        return list(await asyncio.gather(*(fetch(slug, options) for slug, options in selected)))

    async def _persist(self, run: RunRecord, results: list[SourceResult]) -> None:
        for result in results:
            created = 0
            merged = 0
            for job in result.jobs:
                job.stage = PipelineStage.NORMALIZED
                _, was_created = await self._repos.jobs.upsert(job)
                if was_created:
                    created += 1
                else:
                    merged += 1
            run.bump("jobs_discovered", len(result.jobs))
            run.bump("jobs_created", created)
            run.bump("jobs_merged", merged)
            run.bump(f"source.{result.source}.jobs", len(result.jobs))

            await self._repos.sources.record_run(
                result.source, jobs=len(result.jobs), failed=bool(result.failures)
            )
            await self._record_failures(run, result)

            if result.truncated:
                run.failures.append(
                    f"{result.source}: results truncated at the configured per-source limit"
                )

    async def _record_failures(self, run: RunRecord, result: SourceResult) -> None:
        """Persist failures into the run and into job-source memory."""
        for failure in result.failures:
            run.bump("failures")
            run.failures.append(f"{failure.source} [{failure.category.value}]: {failure.message}")
            await self._memory.remember(
                MemoryKind.JOB_SOURCE,
                scope=f"source:{failure.source}",
                key=f"failure.{failure.category.value}.{failure.stage}",
                content=failure.message,
                data={
                    "stage": failure.stage,
                    "strategy": failure.strategy,
                    "retryable": failure.retryable,
                },
                level=AssertionLevel.INFERRED,
                provenance=[
                    Provenance(
                        kind=ProvenanceKind.JOB_SOURCE,
                        origin=failure.source,
                        locator=failure.url,
                    )
                ],
            )

    async def _finish(self, run: RunRecord) -> RunRecord:
        from applyuminati.core.clock import utcnow

        run.finished_at = utcnow()
        if run.state is RunState.RUNNING:
            if run.failures and run.stats.get("jobs_discovered", 0) == 0:
                run.state = RunState.FAILED
            elif run.failures:
                run.state = RunState.PARTIAL
            else:
                run.state = RunState.SUCCEEDED
        log.info(
            "discovery.run_finished",
            run_id=run.id,
            state=run.state.value,
            **{k: v for k, v in run.stats.items() if not k.startswith("source.")},
        )
        return await self._repos.runs.save(run)

    async def verify(self, jobs: list[Job]) -> dict[str, int]:
        """Re-check liveness for the supplied jobs, one source call each.

        Only sources advertising a freshness check are used; everything else
        stays ``UNVERIFIED`` rather than being optimistically marked live.
        """
        from applyuminati.core.clock import utcnow
        from applyuminati.core.models.job import VerificationState
        from applyuminati.sources.base import SourceCapability

        counts: dict[str, int] = {}
        states = await self._repos.sources.all()
        for job in jobs:
            primary = job.primary_source
            if primary is None:
                continue
            state = states.get(primary.source)
            options = dict(state.options) if state else {}
            try:
                source = self._sources.instantiate(primary.source, options)
            except ApplyuminatiError:
                continue
            if not source.metadata.supports(SourceCapability.FRESHNESS_CHECK):
                continue
            try:
                result = await source.verify(job)
            except ApplyuminatiError as exc:
                log.warning("verify.failed", job_id=job.id, error=exc.code)
                result_state = VerificationState.UNKNOWN
            else:
                result_state = result.state
            await self._repos.jobs.record_verification(job.id, result_state, utcnow())
            counts[result_state.value] = counts.get(result_state.value, 0) + 1
        return counts


__all__ = ["DiscoveryService"]
