"""Greenhouse public board API source.

Greenhouse publishes a JSON board API at ``/v1/boards/{board}/jobs`` with no
authentication. This is the cleanest direct-ATS example: structured fields, a
stable schema, and a real freshness check via ``/jobs/{id}``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from applyuminati.core.errors import ApplyuminatiError, FailureCategory
from applyuminati.core.models.common import EmploymentType, Location, RemoteMode
from applyuminati.core.models.job import AtsVendor, Job, SourceTier, VerificationState
from applyuminati.core.registry import HealthReport, HealthState, PluginMaturity
from applyuminati.core.settings import Settings
from applyuminati.sources.base import (
    DiscoveryRequest,
    FreshnessResult,
    JobSource,
    SourceCapability,
    SourceFailure,
    SourceMetadata,
    SourceResult,
    source_plugin,
)
from applyuminati.sources.http import SourceHttpClient
from applyuminati.sources.normalize import build_job

_API = "https://boards-api.greenhouse.io/v1/boards"
_CAPABILITIES = frozenset(
    {
        SourceCapability.LIST_BY_EMPLOYER,
        SourceCapability.FULL_DESCRIPTION,
        SourceCapability.FRESHNESS_CHECK,
        SourceCapability.PAGINATION,
    }
)


class GreenhouseOptions(BaseModel):
    """Board tokens, as they appear in a Greenhouse careers URL."""

    boards: list[str] = Field(default_factory=list)


def _metadata() -> SourceMetadata:
    return SourceMetadata(
        slug="greenhouse",
        name="Greenhouse",
        tier=SourceTier.DIRECT_ATS,
        description="Greenhouse public board API; no authentication required.",
        capabilities=_CAPABILITIES,
        ats=AtsVendor.GREENHOUSE,
    )


class GreenhouseSource(JobSource):
    """Fetches jobs from one or more Greenhouse boards."""

    def __init__(self, settings: Settings, options: dict[str, Any] | None = None) -> None:
        self._settings = settings
        opts = GreenhouseOptions.model_validate(options or {})
        self._boards = opts.boards
        self._client = SourceHttpClient(_metadata(), settings)

    @property
    def metadata(self) -> SourceMetadata:
        return _metadata()

    async def health(self) -> HealthReport:
        if not self._boards:
            return HealthReport(
                plugin="greenhouse",
                state=HealthState.DEGRADED,
                detail="no boards configured; add board tokens to enable discovery",
            )
        try:
            await self._client.get_json(f"{_API}/{self._boards[0]}/jobs")
        except ApplyuminatiError as exc:
            return HealthReport(
                plugin="greenhouse", state=HealthState.UNAVAILABLE, detail=exc.message
            )
        except Exception as exc:
            return HealthReport(plugin="greenhouse", state=HealthState.UNAVAILABLE, detail=str(exc))
        return HealthReport(
            plugin="greenhouse",
            state=HealthState.HEALTHY,
            detail=f"{len(self._boards)} board(s) configured",
        )

    async def discover(self, request: DiscoveryRequest) -> SourceResult:
        jobs: list[Job] = []
        failures: list[SourceFailure] = []
        max_results = request.max_results
        for board in self._boards:
            if len(jobs) >= max_results:
                break
            try:
                payload = await self._client.get_json(
                    f"{_API}/{board}/jobs", params={"content": "true"}
                )
            except ApplyuminatiError as exc:
                failures.append(SourceFailure.from_error("greenhouse", exc, stage="list"))
                continue
            except Exception as exc:
                failures.append(
                    SourceFailure(
                        source="greenhouse",
                        category=FailureCategory.UNKNOWN,
                        message=f"{type(exc).__name__}: {exc}",
                        stage="list",
                    )
                )
                continue
            for raw_job in payload.get("jobs", []):
                if len(jobs) >= max_results:
                    break
                job = self._normalise(board, raw_job)
                if job is not None:
                    jobs.append(job)
        return SourceResult(
            source="greenhouse",
            jobs=jobs,
            failures=failures,
            truncated=len(jobs) >= max_results,
            pages_fetched=len(self._boards),
        )

    async def verify(self, job: Job) -> FreshnessResult:
        record = job.primary_source
        if record is None:
            return FreshnessResult(job_id=job.id, state=VerificationState.UNKNOWN)
        board = str(record.raw.get("board", ""))
        if not board:
            return FreshnessResult(job_id=job.id, state=VerificationState.UNKNOWN)
        try:
            await self._client.get_json(f"{_API}/{board}/jobs/{record.source_job_id}")
        except ApplyuminatiError as exc:
            state = (
                VerificationState.GONE
                if exc.category.value == "endpoint_unavailable"
                else VerificationState.UNKNOWN
            )
            return FreshnessResult(job_id=job.id, state=state, detail=exc.message)
        return FreshnessResult(job_id=job.id, state=VerificationState.LIVE)

    def _normalise(self, board: str, raw: dict[str, Any]) -> Job | None:
        job_id = str(raw.get("id", ""))
        title = raw.get("title")
        if not title or not job_id:
            return None
        url = raw.get("absolute_url") or raw.get("url") or ""
        return build_job(
            source="greenhouse",
            tier=SourceTier.DIRECT_ATS,
            source_job_id=job_id,
            url=url,
            title=title,
            company=raw.get("company_name") or board,
            description=raw.get("content") or raw.get("job_description"),
            locations=self._locations(raw.get("location", {})),
            remote_mode=self._remote_from(raw),
            employment_type=EmploymentType.UNKNOWN,
            apply_url=raw.get("job_apply_url") or url,
            ats=AtsVendor.GREENHOUSE,
            raw={**raw, "board": board},
        )

    def _locations(self, location: dict[str, Any]) -> list[Location]:
        name = location.get("name")
        return [Location(raw=name)] if name else []

    def _remote_from(self, raw: dict[str, Any]) -> RemoteMode:
        if "remote" in (raw.get("location", {}).get("name", "") or "").lower():
            return RemoteMode.REMOTE
        return RemoteMode.UNKNOWN

    async def aclose(self) -> None:
        await self._client.aclose()


PLUGIN = source_plugin(
    slug="greenhouse",
    name="Greenhouse",
    factory=GreenhouseSource,
    description="Greenhouse public board API; no authentication required.",
    capabilities=_CAPABILITIES,
    options_schema=GreenhouseOptions,
    priority=10,
    maturity=PluginMaturity.WORKFLOW_INTEGRATED,
)
