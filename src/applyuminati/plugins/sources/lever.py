"""Lever public postings API source.

Lever exposes ``/v0/postings/{company}?mode=json`` with no authentication and
a stable schema. It complements Greenhouse as a second direct-ATS adapter and
proves the plugin boundary across two different vendor payload shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime
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

_API = "https://api.lever.co/v0/postings"
_CAPABILITIES = frozenset(
    {
        SourceCapability.LIST_BY_EMPLOYER,
        SourceCapability.FULL_DESCRIPTION,
        SourceCapability.FRESHNESS_CHECK,
    }
)


class LeverOptions(BaseModel):
    companies: list[str] = Field(default_factory=list)


def _metadata() -> SourceMetadata:
    return SourceMetadata(
        slug="lever",
        name="Lever",
        tier=SourceTier.DIRECT_ATS,
        description="Lever public postings API; no authentication required.",
        capabilities=_CAPABILITIES,
        ats=AtsVendor.LEVER,
    )


class LeverSource(JobSource):
    def __init__(self, settings: Settings, options: dict[str, Any] | None = None) -> None:
        self._settings = settings
        opts = LeverOptions.model_validate(options or {})
        self._companies = opts.companies
        self._client = SourceHttpClient(_metadata(), settings)

    @property
    def metadata(self) -> SourceMetadata:
        return _metadata()

    async def health(self) -> HealthReport:
        if not self._companies:
            return HealthReport(
                plugin="lever",
                state=HealthState.DEGRADED,
                detail="no companies configured",
            )
        try:
            await self._client.get_json(f"{_API}/{self._companies[0]}", params={"mode": "json"})
        except ApplyuminatiError as exc:
            return HealthReport(plugin="lever", state=HealthState.UNAVAILABLE, detail=exc.message)
        except Exception as exc:
            return HealthReport(plugin="lever", state=HealthState.UNAVAILABLE, detail=str(exc))
        return HealthReport(
            plugin="lever",
            state=HealthState.HEALTHY,
            detail=f"{len(self._companies)} company site(s) configured",
        )

    async def discover(self, request: DiscoveryRequest) -> SourceResult:
        jobs: list[Job] = []
        failures: list[SourceFailure] = []
        for company in self._companies:
            if len(jobs) >= request.max_results:
                break
            try:
                postings = await self._client.get_json(f"{_API}/{company}", params={"mode": "json"})
            except ApplyuminatiError as exc:
                failures.append(SourceFailure.from_error("lever", exc, stage="list"))
                continue
            except Exception as exc:
                failures.append(
                    SourceFailure(
                        source="lever",
                        category=FailureCategory.UNKNOWN,
                        message=f"{type(exc).__name__}: {exc}",
                        stage="list",
                    )
                )
                continue
            for raw in postings if isinstance(postings, list) else []:
                if len(jobs) >= request.max_results:
                    break
                job = self._normalise(company, raw)
                if job is not None:
                    jobs.append(job)
        return SourceResult(
            source="lever",
            jobs=jobs,
            failures=failures,
            truncated=len(jobs) >= request.max_results,
            pages_fetched=len(self._companies),
        )

    async def verify(self, job: Job) -> FreshnessResult:
        record = job.primary_source
        if record is None:
            return FreshnessResult(job_id=job.id, state=VerificationState.UNKNOWN)
        company = str(record.raw.get("company", ""))
        if not company:
            return FreshnessResult(job_id=job.id, state=VerificationState.UNKNOWN)
        try:
            await self._client.get_json(f"{_API}/{company}/{record.source_job_id}")
        except ApplyuminatiError as exc:
            state = (
                VerificationState.GONE
                if exc.category.value == "endpoint_unavailable"
                else VerificationState.UNKNOWN
            )
            return FreshnessResult(job_id=job.id, state=state, detail=exc.message)
        return FreshnessResult(job_id=job.id, state=VerificationState.LIVE)

    def _normalise(self, company: str, raw: dict[str, Any]) -> Job | None:
        posting_id = raw.get("id")
        text = raw.get("text")
        if not posting_id or not text:
            return None
        categories = raw.get("categories", {}) or {}
        location_name = categories.get("location") or raw.get("locationName")
        locations = [Location(raw=location_name)] if location_name else []
        commitment = categories.get("commitment") or raw.get("workplaceType")
        created_at = raw.get("createdAt")
        posted_at = datetime.fromtimestamp(int(created_at) / 1000, tz=UTC) if created_at else None
        description_parts: list[str] = []
        desc = raw.get("description")
        if desc:
            description_parts.append(desc)
        for block in raw.get("content", {}).get("sections", []):
            if block.get("text"):
                description_parts.append(block["text"])
        description = "\n\n".join(description_parts) or None
        return build_job(
            source="lever",
            tier=SourceTier.DIRECT_ATS,
            source_job_id=str(posting_id),
            url=raw.get("hostedUrl") or raw.get("applyUrl") or "",
            title=text,
            company=raw.get("ownerName") or company,
            description=description,
            locations=locations,
            remote_mode=self._remote_from(commitment, raw),
            employment_type=self._employment_from(commitment),
            posted_at=posted_at,
            apply_url=raw.get("applyUrl"),
            ats=AtsVendor.LEVER,
            raw={**raw, "company": company},
        )

    def _remote_from(self, commitment: str | None, raw: dict[str, Any]) -> RemoteMode:
        workplace = (raw.get("workplaceType") or "").lower()
        if "remote" in workplace:
            return RemoteMode.REMOTE
        if "hybrid" in workplace:
            return RemoteMode.HYBRID
        if "onsite" in workplace:
            return RemoteMode.ONSITE
        if commitment and "remote" in commitment.lower():
            return RemoteMode.REMOTE
        return RemoteMode.UNKNOWN

    def _employment_from(self, commitment: str | None) -> EmploymentType:
        if not commitment:
            return EmploymentType.UNKNOWN
        lowered = commitment.lower()
        if "full" in lowered:
            return EmploymentType.FULL_TIME
        if "part" in lowered:
            return EmploymentType.PART_TIME
        if "contract" in lowered:
            return EmploymentType.CONTRACT
        if "intern" in lowered:
            return EmploymentType.INTERNSHIP
        return EmploymentType.UNKNOWN

    async def aclose(self) -> None:
        await self._client.aclose()


PLUGIN = source_plugin(
    slug="lever",
    name="Lever",
    factory=LeverSource,
    description="Lever public postings API; no authentication required.",
    capabilities=_CAPABILITIES,
    options_schema=LeverOptions,
    priority=9,
    maturity=PluginMaturity.WORKFLOW_INTEGRATED,
)
