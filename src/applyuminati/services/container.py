"""Service container.

One object that owns process-wide resources — settings, the database, plugin
registries, the LLM client — and hands out per-request service instances bound
to a session. The API and the CLI both build one, which is how architectural
rule 3 ("UI and CLI use the same application services") is enforced
structurally rather than by convention.

Registration of builtin plugins happens exactly once here, at construction.
Nothing below the services layer ever imports a concrete plugin.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from applyuminati.core.logging import configure_logging, get_logger
from applyuminati.core.settings import Settings, get_settings
from applyuminati.db.repositories import (
    ApplicationRepository,
    JobRepository,
    LLMCallRepository,
    MemoryRepository,
    ProfileRepository,
    ResearchRepository,
    RunRepository,
    ScoreRepository,
    SourceStateRepository,
    TaskRepository,
)
from applyuminati.db.session import Database
from applyuminati.llm.client import LLMClient

log = get_logger(__name__)


@dataclass(slots=True)
class Repositories:
    """Every repository bound to one session.

    Passed as a unit so a service method that touches three aggregates does so
    inside a single transaction instead of opening three sessions.
    """

    session: AsyncSession
    profiles: ProfileRepository
    jobs: JobRepository
    scores: ScoreRepository
    applications: ApplicationRepository
    memory: MemoryRepository
    sources: SourceStateRepository
    runs: RunRepository
    tasks: TaskRepository
    research: ResearchRepository
    llm_calls: LLMCallRepository

    @classmethod
    def bind(cls, session: AsyncSession) -> Repositories:
        return cls(
            session=session,
            profiles=ProfileRepository(session),
            jobs=JobRepository(session),
            scores=ScoreRepository(session),
            applications=ApplicationRepository(session),
            memory=MemoryRepository(session),
            sources=SourceStateRepository(session),
            runs=RunRepository(session),
            tasks=TaskRepository(session),
            research=ResearchRepository(session),
            llm_calls=LLMCallRepository(session),
        )


class ServiceContainer:
    """Process-wide wiring. Build one; share it."""

    def __init__(self, settings: Settings | None = None, *, database: Database | None = None) -> None:
        self.settings = settings or get_settings()
        configure_logging(level=self.settings.log_level, fmt=self.settings.log_format)
        self.settings.ensure_directories()
        self.database = database or Database(self.settings)
        self._llm: LLMClient | None = None
        _register_builtin_plugins()

    # -- resources --------------------------------------------------------

    @property
    def llm(self) -> LLMClient:
        """Lazily built LLM client.

        Lazy because a user with no provider configured must still be able to
        boot, discover jobs and score them deterministically.
        """
        if self._llm is None:
            self._llm = LLMClient.from_settings(self.settings)
        return self._llm

    @asynccontextmanager
    async def repositories(self) -> AsyncIterator[Repositories]:
        """Transactional unit of work."""
        async with self.database.session() as session:
            yield Repositories.bind(session)

    @asynccontextmanager
    async def read_repositories(self) -> AsyncIterator[Repositories]:
        """Read-only unit of work; never commits."""
        async with self.database.read_session() as session:
            yield Repositories.bind(session)

    async def aclose(self) -> None:
        if self._llm is not None:
            await self._llm.aclose()
            self._llm = None
        await self.database.dispose()


def _register_builtin_plugins() -> None:
    """Register first-party adapters. Idempotent, safe to call repeatedly.

    Bootstrap lives in the plugin packages rather than in the contract
    packages: ``applyuminati.sources`` importing ``applyuminati.plugins.sources``
    would invert the dependency the whole plugin architecture exists to
    protect. The services layer is the lowest layer permitted to know that
    concrete adapters exist, so it is the right place to wire them up.
    """
    from applyuminati.plugins.agents import register_agents
    from applyuminati.plugins.browsers import register_browsers
    from applyuminati.plugins.email import register_email_providers
    from applyuminati.plugins.llm import register_llm_providers
    from applyuminati.plugins.sources import register_sources

    register_sources()
    register_llm_providers()
    register_browsers()
    register_agents()
    register_email_providers()


_container: ServiceContainer | None = None


def get_container(settings: Settings | None = None) -> ServiceContainer:
    """Return the process container, creating it on first use."""
    global _container  # noqa: PLW0603 - deliberate process-wide singleton
    if _container is None:
        _container = ServiceContainer(settings)
    return _container


def set_container(container: ServiceContainer | None) -> None:
    """Replace (or clear) the process container. Used by tests and app startup."""
    global _container  # noqa: PLW0603
    _container = container


__all__ = ["Repositories", "ServiceContainer", "get_container", "set_container"]
