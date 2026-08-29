"""Profile endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from applyuminati.api.dependencies import get_repositories
from applyuminati.api.mappers import profile_to_dto
from applyuminati.api.schemas import (
    PreferencesUpdateRequest,
    ProfileImportRequest,
    ProfileImportResponse,
    ProfileResponse,
)
from applyuminati.core.errors import ConfigurationError, NotFoundError
from applyuminati.services.container import Repositories
from applyuminati.services.profile_service import ProfileService

router = APIRouter(prefix="/api/v1/profile", tags=["profile"])


@router.get("", response_model=ProfileResponse)
async def get_profile(repos: Repositories = Depends(get_repositories)) -> ProfileResponse:
    svc = ProfileService(repos)
    try:
        profile = await svc.get()
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    view = await svc.view()
    return profile_to_dto(
        view, strategy=profile.strategy, targets=profile.targets.model_dump(mode="json")
    )


@router.post("/import", response_model=ProfileImportResponse)
async def import_profile(
    request: ProfileImportRequest,
    repos: Repositories = Depends(get_repositories),
) -> ProfileImportResponse:
    svc = ProfileService(repos)
    try:
        result = await svc.import_resume(
            request.resume, label=request.label, replace=request.replace
        )
    except ConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    profile = await svc.get()
    return ProfileImportResponse(
        profile=profile_to_dto(
            result.profile,
            strategy=profile.strategy,
            targets=profile.targets.model_dump(mode="json"),
        ),
        claims_created=result.claims_created,
        metrics_extracted=result.metrics_extracted,
        warnings=result.warnings,
    )


@router.put("/preferences", response_model=ProfileResponse)
async def update_preferences(
    request: PreferencesUpdateRequest,
    repos: Repositories = Depends(get_repositories),
) -> ProfileResponse:
    svc = ProfileService(repos)
    try:
        profile = await svc.update_preferences(
            titles=request.titles,
            locations=request.locations,
            remote_modes=request.remote_modes,
            employment_types=request.employment_types,
            seniority=request.seniority,
            minimum_compensation=request.minimum_compensation,
            compensation_currency=request.compensation_currency,
            strategy=request.strategy,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    view = await svc.view()
    return profile_to_dto(
        view, strategy=profile.strategy, targets=profile.targets.model_dump(mode="json")
    )
