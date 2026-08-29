"""Sources endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from applyuminati.api.dependencies import get_container_dep
from applyuminati.api.mappers import source_to_dto
from applyuminati.api.schemas import SourceInfo, SourceToggleRequest
from applyuminati.core.errors import ConfigurationError
from applyuminati.services.container import ServiceContainer
from applyuminati.services.source_service import SourceService

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


@router.get("", response_model=list[SourceInfo])
async def list_sources(
    container: ServiceContainer = Depends(get_container_dep),
) -> list[SourceInfo]:
    async with container.repositories() as repos:
        svc = SourceService(repos, container.settings)
        views = await svc.list(probe_health=True)
    return [source_to_dto(view) for view in views]


@router.post("/{slug}/enable", response_model=SourceInfo)
async def enable_source(
    slug: str,
    request: SourceToggleRequest,
    container: ServiceContainer = Depends(get_container_dep),
) -> SourceInfo:
    async with container.repositories() as repos:
        svc = SourceService(repos, container.settings)
        try:
            view = await svc.set_enabled(slug, True, options=request.options)
        except ConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message
            ) from exc
    return source_to_dto(view)


@router.post("/{slug}/disable", response_model=SourceInfo)
async def disable_source(
    slug: str,
    container: ServiceContainer = Depends(get_container_dep),
) -> SourceInfo:
    async with container.repositories() as repos:
        svc = SourceService(repos, container.settings)
        try:
            view = await svc.set_enabled(slug, False)
        except ConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message
            ) from exc
    return source_to_dto(view)
