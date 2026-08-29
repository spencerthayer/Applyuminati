"""Jobs endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from applyuminati.api.dependencies import get_container_dep, get_repositories
from applyuminati.api.mappers import job_to_detail, job_to_summary
from applyuminati.api.schemas import JobDetail, JobSummary, Page
from applyuminati.core.errors import NotFoundError
from applyuminati.core.models.common import RemoteMode
from applyuminati.core.models.job import VerificationState
from applyuminati.core.models.scoring import Recommendation
from applyuminati.services.container import Repositories, ServiceContainer
from applyuminati.services.job_service import JobService

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("", response_model=Page[JobSummary])
async def list_jobs(
    repos: Repositories = Depends(get_repositories),
    query: str | None = Query(None),
    source: list[str] = Query(default_factory=list),
    recommendation: Recommendation | None = Query(None),
    min_score: float | None = Query(None, ge=0.0, le=1.0),
    state: list[str] = Query(default_factory=list),
    company: list[str] = Query(default_factory=list),
    remote_mode: RemoteMode | None = Query(None),
    verification: VerificationState | None = Query(None),
    has_score: bool | None = Query(None),
    sort: str = Query("discovered_at"),
    descending: bool = Query(True),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[JobSummary]:
    svc = JobService(repos)
    page = await svc.list(
        query=query,
        sources=source or None,
        recommendation=recommendation,
        min_score=min_score,
        states=None,
        companies=company or None,
        remote_modes=[remote_mode] if remote_mode else None,
        verification=verification,
        has_score=has_score,
        sort=sort,
        descending=descending,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[job_to_summary(view) for view in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{job_id}", response_model=JobDetail)
async def get_job(
    job_id: str,
    repos: Repositories = Depends(get_repositories),
) -> JobDetail:
    svc = JobService(repos)
    try:
        view = await svc.get(job_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    return job_to_detail(view)


@router.post("/discover")
async def discover_jobs(
    container: ServiceContainer = Depends(get_container_dep),
    sources: list[str] = Query(default_factory=list),
    queries: list[str] = Query(default_factory=list),
    locations: list[str] = Query(default_factory=list),
) -> dict:
    from applyuminati.services.discovery_service import DiscoveryService

    async with container.repositories() as repos:
        svc = DiscoveryService(repos, container.settings)
        run = await svc.discover(
            sources=sources or None, queries=queries or None, locations=locations or None
        )
    return {
        "run_id": run.id,
        "state": run.state.value,
        "jobs_discovered": run.stats.get("jobs_discovered", 0),
        "jobs_created": run.stats.get("jobs_created", 0),
        "jobs_merged": run.stats.get("jobs_merged", 0),
        "failures": run.failures,
    }


@router.post("/score")
async def score_jobs(
    container: ServiceContainer = Depends(get_container_dep),
    job_ids: list[str] = Query(default_factory=list),
    rescore: bool = Query(False),
    use_llm: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    from applyuminati.services.scoring_service import ScoringService

    async with container.repositories() as repos:
        svc = ScoringService(repos, container.settings, container.llm)
        run = await svc.score_jobs(
            job_ids=job_ids or None, rescore=rescore, use_llm=use_llm, limit=limit
        )
    return {
        "run_id": run.id,
        "state": run.state.value,
        "scored": run.stats.get("scored", 0),
        "failed": run.stats.get("failed", 0),
        "failures": run.failures,
    }
