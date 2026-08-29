"""Health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from applyuminati.api.dependencies import get_container_dep
from applyuminati.api.schemas import (
    BackendHealthResponse,
    HealthResponse,
)
from applyuminati.api.mappers import backend_health_to_dto
from applyuminati.core.clock import utcnow
from applyuminati.services.container import ServiceContainer

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("", response_model=HealthResponse)
async def health(container: ServiceContainer = Depends(get_container_dep)) -> HealthResponse:
    db_ok = await container.database.check()
    schema_version = await container.database.schema_version()
    async with container.repositories() as repos:
        from applyuminati.services.health_service import HealthService

        svc = HealthService(repos, container.settings, container._llm)
        summary = await svc.summary(db_ok, schema_version)
    from applyuminati import __version__

    return HealthResponse(
        status=summary["status"],
        version=__version__,
        database_ok=summary["database_ok"],
        schema_version=summary.get("schema_version"),
        execution_mode=summary["execution_mode"],
        profile_configured=summary["profile_configured"],
        enabled_sources=summary.get("enabled_sources", []),
        checked_at=utcnow(),
    )


@router.get("/backends", response_model=BackendHealthResponse)
async def backend_health(
    container: ServiceContainer = Depends(get_container_dep),
) -> BackendHealthResponse:
    async with container.repositories() as repos:
        from applyuminati.services.health_service import HealthService

        svc = HealthService(repos, container.settings, container._llm)
        view = await svc.backends()
    return backend_health_to_dto(view)
