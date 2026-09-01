"""Needs-you inbox: open human interventions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from applyuminati.api.dependencies import get_container_dep, get_repositories
from applyuminati.core.errors import NotFoundError
from applyuminati.core.models.execution import InterventionResolution
from applyuminati.services.attempt_service import AttemptService, host_presence
from applyuminati.services.container import Repositories, ServiceContainer

router = APIRouter(prefix="/api/v1/needs-you", tags=["needs-you"])


class InboxEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    application_id: str
    job_id: str
    company: str | None = None
    title: str | None = None
    intervention_id: str
    reason: str
    instruction: str
    requires_browser_handoff: bool
    question_text: str | None = None
    browser_host_id: str | None = None
    browser_session_id: str | None = None
    task_space_id: str | None = None
    host_presence: str = "not_required"
    opened_at: datetime


class OpenBrowserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    host_presence: str
    task_space_id: str | None = None
    detail: str


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: InterventionResolution
    payload: dict[str, Any] = Field(default_factory=dict)


class ResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    workflow_state: str
    open_intervention: str | None = None


@router.get("", response_model=list[InboxEntry])
async def list_inbox(
    repos: Repositories = Depends(get_repositories),
    container: ServiceContainer = Depends(get_container_dep),
) -> list[InboxEntry]:
    items = await AttemptService(repos).inbox()
    manager = container.browser_hosts
    return [
        InboxEntry(
            attempt_id=item.attempt.id,
            application_id=item.attempt.application_id,
            job_id=item.attempt.job_id,
            company=item.company,
            title=item.title,
            intervention_id=item.intervention.id,
            reason=item.intervention.reason.value,
            instruction=item.intervention.instruction,
            requires_browser_handoff=item.intervention.requires_browser_handoff,
            question_text=item.intervention.question_text,
            browser_host_id=item.intervention.browser_host_id,
            browser_session_id=item.intervention.browser_session_id,
            task_space_id=item.intervention.task_space_id,
            host_presence=host_presence(item.attempt, item.intervention, manager).value,
            opened_at=item.intervention.opened_at,
        )
        for item in items
    ]


@router.post("/{attempt_id}/open-browser", response_model=OpenBrowserResponse)
async def open_browser(
    attempt_id: str,
    repos: Repositories = Depends(get_repositories),
    container: ServiceContainer = Depends(get_container_dep),
) -> OpenBrowserResponse:
    service = AttemptService(repos)
    try:
        attempt = await service.get(attempt_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    intervention = attempt.pending_intervention
    instruction = (
        intervention.instruction if intervention is not None else "Take over this application."
    )
    result = await service.activate_browser(
        attempt, manager=container.browser_hosts, instruction=instruction
    )
    return OpenBrowserResponse(
        ok=bool(result["ok"]),
        host_presence=str(result["host_presence"]),
        task_space_id=result.get("task_space_id"),
        detail=str(result["detail"]),
    )


@router.post("/{attempt_id}/{intervention_id}", response_model=ResolveResponse)
async def resolve_inbox(
    attempt_id: str,
    intervention_id: str,
    request: ResolveRequest,
    repos: Repositories = Depends(get_repositories),
    container: ServiceContainer = Depends(get_container_dep),
) -> ResolveResponse:
    try:
        attempt = await AttemptService(repos).resolve(
            attempt_id,
            intervention_id,
            request.resolution,
            payload=request.payload,
            manager=container.browser_hosts,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    open_item = attempt.pending_intervention
    return ResolveResponse(
        attempt_id=attempt.id,
        workflow_state=attempt.workflow_state.value,
        open_intervention=open_item.id if open_item else None,
    )
