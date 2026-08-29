"""Uniform error handling: ApplyuminatiError -> ErrorResponse."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from applyuminati.api.schemas import ErrorResponse
from applyuminati.core.errors import ApplyuminatiError
from applyuminati.core.logging import get_logger

log = get_logger(__name__)


async def applyuminati_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApplyuminatiError):
        raise exc
    payload = ErrorResponse(**exc.to_dict())
    log.warning(
        "api.error",
        code=exc.code,
        category=exc.category.value,
        path=request.url.path,
    )
    status = _status_for_category(exc.category.value)
    return JSONResponse(status_code=status, content=payload.model_dump(mode="json"))


def _status_for_category(category: str) -> int:
    return {
        "configuration": 400,
        "storage": 500,
        "backend_unavailable": 503,
        "auth_required": 401,
        "automation_blocked": 403,
        "human_challenge": 451,
        "rate_limited": 429,
        "resource_gone": 404,
        "needs_human": 422,
        "policy_refused": 422,
        "duplicate_action": 409,
    }.get(category, 500)
