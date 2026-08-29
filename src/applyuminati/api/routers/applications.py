"""Applications endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from applyuminati.api.dependencies import get_repositories
from applyuminati.api.mappers import application_to_detail, application_to_summary
from applyuminati.api.schemas import (
    ApplicationDetail,
    ApplicationSummary,
    Page,
    TransitionRequest,
)
from applyuminati.applications.machine import IllegalTransitionError
from applyuminati.core.errors import NotFoundError
from applyuminati.core.models.application import ApplicationState
from applyuminati.services.application_service import ApplicationService
from applyuminati.services.container import Repositories

router = APIRouter(prefix="/api/v1/applications", tags=["applications"])


@router.get("", response_model=Page[ApplicationSummary])
async def list_applications(
    repos: Repositories = Depends(get_repositories),
    state: list[ApplicationState] = Query(default_factory=list),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[ApplicationSummary]:
    svc = ApplicationService(repos)
    page = await svc.list(states=state or None, limit=limit, offset=offset)
    return Page(
        items=[application_to_summary(view) for view in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{application_id}", response_model=ApplicationDetail)
async def get_application(
    application_id: str,
    repos: Repositories = Depends(get_repositories),
) -> ApplicationDetail:
    svc = ApplicationService(repos)
    try:
        view = await svc.get(application_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    return application_to_detail(view)


@router.post("/{application_id}/transition", response_model=ApplicationDetail)
async def transition_application(
    application_id: str,
    request: TransitionRequest,
    repos: Repositories = Depends(get_repositories),
) -> ApplicationDetail:
    svc = ApplicationService(repos)
    try:
        view = await svc.transition(
            application_id,
            request.to_state,
            reason=request.reason,
            message=request.message,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except IllegalTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        ) from exc
    return application_to_detail(view)
