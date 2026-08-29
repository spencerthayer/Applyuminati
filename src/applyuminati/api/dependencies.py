"""FastAPI dependencies: request-scoped services from the process container."""

from __future__ import annotations

from collections.abc import AsyncIterator

from applyuminati.core.settings import Settings
from applyuminati.services.container import Repositories, ServiceContainer, get_container

__all__ = ["get_container_dep", "get_repositories", "get_settings"]


async def get_container_dep() -> AsyncIterator[ServiceContainer]:
    """Yield the process container. FastAPI calls this per request."""
    container = get_container()
    yield container


async def get_repositories() -> AsyncIterator[Repositories]:
    """Yield a transactional unit of work for one request."""
    container = get_container()
    async with container.repositories() as repos:
        yield repos


def get_settings() -> Settings:
    return get_container().settings
