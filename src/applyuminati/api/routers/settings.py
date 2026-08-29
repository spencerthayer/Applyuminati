"""Settings and dashboard endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from applyuminati.api.dependencies import get_container_dep, get_repositories
from applyuminati.api.mappers import dashboard_to_dto
from applyuminati.api.schemas import (
    DashboardResponse,
    SettingsResponse,
    StrategyUpdateRequest,
)
from applyuminati.core.errors import ConfigurationError
from applyuminati.services.container import Repositories, ServiceContainer
from applyuminati.services.dashboard_service import DashboardService
from applyuminati.services.settings_service import SettingsService

dashboard_router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
settings_router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


@dashboard_router.get("", response_model=DashboardResponse)
async def dashboard(
    repos: Repositories = Depends(get_repositories),
) -> DashboardResponse:
    svc = DashboardService(repos)
    view = await svc.build()
    return dashboard_to_dto(view)


@settings_router.get("", response_model=SettingsResponse)
async def get_settings(
    container: ServiceContainer = Depends(get_container_dep),
) -> SettingsResponse:
    async with container.repositories() as repos:
        svc = SettingsService(repos, container.settings)
        snapshot = await svc.snapshot()
    return SettingsResponse(**snapshot)


@settings_router.put("/strategy")
async def update_strategy(
    request: StrategyUpdateRequest,
    container: ServiceContainer = Depends(get_container_dep),
) -> dict:
    async with container.repositories() as repos:
        svc = SettingsService(repos, container.settings)
        try:
            strategy = await svc.update_strategy(
                strategy=request.strategy, preset_name=request.preset
            )
        except ConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message
            ) from exc
    return {"strategy": strategy.model_dump(mode="json")}
