"""Local file feed source.

Reads ``.json`` and ``.jsonl`` files from disk so a user can paste postings,
wire up their own scraper, or load a niche board's export. This adapter proves
the plugin boundary works for a non-HTTP transport and makes the whole
discovery path testable offline — its presence is why the vertical slice has
no hard dependency on any live job board.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from applyuminati.core.errors import FailureCategory
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
from applyuminati.sources.normalize import build_job

_CAPABILITIES = frozenset({SourceCapability.OFFLINE, SourceCapability.FULL_DESCRIPTION})


class LocalFeedOptions(BaseModel):
    paths: list[Path] = Field(default_factory=list)
    default_company: str | None = None


def _metadata() -> SourceMetadata:
    return SourceMetadata(
        slug="local_feed",
        name="Local feed",
        tier=SourceTier.DERIVED,
        description="Load postings from local .json/.jsonl files.",
        capabilities=_CAPABILITIES,
        ats=AtsVendor.UNKNOWN,
    )


class LocalFeedSource(JobSource):
    def __init__(self, settings: Settings, options: dict[str, Any] | None = None) -> None:
        self._settings = settings
        opts = LocalFeedOptions.model_validate(options or {})
        self._paths = opts.paths
        self._default_company = opts.default_company

    @property
    def metadata(self) -> SourceMetadata:
        return _metadata()

    async def health(self) -> HealthReport:
        if not self._paths:
            return HealthReport(
                plugin="local_feed",
                state=HealthState.DEGRADED,
                detail="no file paths configured",
            )
        missing = [str(path) for path in self._paths if not path.exists()]
        if missing:
            return HealthReport(
                plugin="local_feed",
                state=HealthState.DEGRADED,
                detail=f"missing files: {', '.join(missing[:3])}",
            )
        return HealthReport(
            plugin="local_feed",
            state=HealthState.HEALTHY,
            detail=f"{len(self._paths)} file(s) reachable",
        )

    async def discover(self, request: DiscoveryRequest) -> SourceResult:
        jobs: list[Job] = []
        failures: list[SourceFailure] = []
        max_results = request.max_results
        for path in self._paths:
            if len(jobs) >= max_results:
                break
            try:
                raw_jobs = self._load(path)
            except (OSError, json.JSONDecodeError) as exc:
                failures.append(
                    SourceFailure(
                        source="local_feed",
                        category=FailureCategory.ENDPOINT_UNAVAILABLE,
                        message=f"could not read {path}: {exc}",
                        stage="list",
                    )
                )
                continue
            for raw in raw_jobs:
                if len(jobs) >= max_results:
                    break
                job = self._normalise(raw)
                if job is not None:
                    jobs.append(job)
        return SourceResult(
            source="local_feed",
            jobs=jobs,
            failures=failures,
            truncated=len(jobs) >= max_results,
        )

    async def verify(self, job: Job) -> FreshnessResult:
        # A local file cannot prove a posting is still live.
        return FreshnessResult(
            job_id=job.id, state=VerificationState.UNKNOWN, detail="local feed; liveness unknown"
        )

    def _load(self, path: Path) -> list[dict[str, Any]]:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        data = json.loads(text)
        if isinstance(data, dict):
            return data.get("jobs", []) if "jobs" in data else [data]
        if isinstance(data, list):
            return data
        return []

    def _normalise(self, raw: dict[str, Any]) -> Job | None:
        source_job_id = str(raw.get("id") or raw.get("source_job_id") or raw.get("url") or "")
        title = raw.get("title")
        company = raw.get("company") or self._default_company
        url = raw.get("url") or raw.get("canonical_url") or ""
        if not title or not company or not url:
            return None
        locations = [Location(raw=loc) for loc in raw.get("locations", []) if loc] or (
            [Location(raw=raw.get("location"))] if raw.get("location") else []
        )
        return build_job(
            source="local_feed",
            tier=SourceTier.DERIVED,
            source_job_id=source_job_id or url,
            url=url,
            title=title,
            company=company,
            description=raw.get("description"),
            locations=locations,
            remote_mode=RemoteMode(raw["remote_mode"]) if raw.get("remote_mode") else None,
            employment_type=EmploymentType(raw["employment_type"])
            if raw.get("employment_type")
            else None,
            posted_at=raw.get("posted_at"),
            apply_url=raw.get("apply_url"),
            ats=AtsVendor(raw.get("ats", "unknown")),
            raw=raw,
        )

    async def aclose(self) -> None:
        pass


PLUGIN = source_plugin(
    slug="local_feed",
    name="Local feed",
    factory=LocalFeedSource,
    description="Load postings from local .json/.jsonl files.",
    capabilities=_CAPABILITIES,
    options_schema=LocalFeedOptions,
    priority=1,
    maturity=PluginMaturity.WORKFLOW_INTEGRATED,
)
