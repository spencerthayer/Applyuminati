"""A user must be able to start an application, and only one at a time."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from applyuminati.api.app import create_app
from applyuminati.core.errors import (
    ConfigurationError,
    DuplicateActionError,
    NotFoundError,
)
from applyuminati.core.models.execution import WorkflowState
from applyuminati.core.models.job import Job, SourceTier
from applyuminati.core.models.jsonresume import JsonResume, ResumeBasics
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.settings import ExecutionMode, SecuritySettings
from applyuminati.db.repositories.jobs import JobRepository
from applyuminati.db.session import set_database
from applyuminati.services.attempt_service import AttemptService
from applyuminati.services.attempt_tasks import APPLICATION_ATTEMPT_KIND
from applyuminati.services.container import Repositories, set_container
from applyuminati.sources.normalize import build_job


def _job() -> Job:
    return build_job(
        source="linkedin",
        tier=SourceTier.AGGREGATOR,
        source_job_id="99",
        url="https://www.linkedin.com/jobs/view/99",
        title="Staff Engineer",
        company="Acme",
        apply_url="https://boards.greenhouse.io/acme/jobs/99",
    )


def _profile() -> CareerProfile:
    return CareerProfile(id="p1", label="test", resume=JsonResume(basics=ResumeBasics(name="Test")))


async def _seed(database, *, with_profile: bool = True) -> Job:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        if with_profile:
            await repos.profiles.upsert(_profile())
    return job


async def test_start_for_job_creates_an_attempt_and_queues_it(database) -> None:
    job = await _seed(database)
    async with database.session() as session:
        repos = Repositories.bind(session)
        attempt = await AttemptService(repos).start_for_job(
            job_id=job.id, profile=None, mode=ExecutionMode.RESEARCH_ONLY
        )
        assert attempt.job_id == job.id
        assert attempt.workflow_state is WorkflowState.PENDING
        tasks = await repos.tasks.list()
        assert [t.kind for t in tasks] == [APPLICATION_ATTEMPT_KIND]
        assert tasks[0].payload == {"attempt_id": attempt.id}


async def test_start_for_job_refuses_a_second_in_flight_attempt(database) -> None:
    """A second start must not queue a second concurrent attempt for one job."""
    job = await _seed(database)
    async with database.session() as session:
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        await service.start_for_job(job_id=job.id, profile=None, mode=ExecutionMode.RESEARCH_ONLY)
        with pytest.raises(DuplicateActionError):
            await service.start_for_job(
                job_id=job.id, profile=None, mode=ExecutionMode.RESEARCH_ONLY
            )
        assert len(await repos.tasks.list()) == 1


async def test_start_for_job_allows_a_restart_once_the_attempt_is_terminal(database) -> None:
    """A finished attempt is not in flight, so the job may be applied to again."""
    job = await _seed(database)
    async with database.session() as session:
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        first = await service.start_for_job(
            job_id=job.id, profile=None, mode=ExecutionMode.RESEARCH_ONLY
        )
        first.workflow_state = WorkflowState.CANCELLED
        await repos.attempts.save(first)
        second = await service.start_for_job(
            job_id=job.id, profile=None, mode=ExecutionMode.RESEARCH_ONLY
        )
        assert second.id != first.id


async def test_start_for_job_rejects_an_unknown_job(database) -> None:
    async with database.session() as session:
        service = AttemptService(Repositories.bind(session))
        with pytest.raises(NotFoundError):
            await service.start_for_job(
                job_id="nope", profile=None, mode=ExecutionMode.RESEARCH_ONLY
            )


async def test_start_for_job_requires_a_profile(database) -> None:
    job = await _seed(database, with_profile=False)
    async with database.session() as session:
        with pytest.raises(ConfigurationError):
            await AttemptService(Repositories.bind(session)).start_for_job(
                job_id=job.id, profile=None, mode=ExecutionMode.RESEARCH_ONLY
            )


@asynccontextmanager
async def _http(database):
    """An ASGI client bound to this test's container and database."""
    from httpx import ASGITransport, AsyncClient

    set_container(None)
    set_database(database)
    app = create_app(
        database.settings.model_copy(update={"security": SecuritySettings(enabled=False)})
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_apply_route_starts_an_application(database) -> None:
    """AC-2: the route a UI button calls returns an attempt."""
    job = await _seed(database)
    async with _http(database) as client:
        response = await client.post(f"/api/v1/jobs/{job.id}/apply", json={"mode": "research_only"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["attempt_id"]
    assert body["state"] == WorkflowState.PENDING.value


async def test_apply_route_returns_404_for_an_unknown_job(database) -> None:
    async with _http(database) as client:
        response = await client.post("/api/v1/jobs/nope/apply", json={})
    assert response.status_code == 404


async def test_apply_route_returns_409_when_one_is_in_flight(database) -> None:
    job = await _seed(database)
    async with _http(database) as client:
        first = await client.post(f"/api/v1/jobs/{job.id}/apply", json={})
        assert first.status_code == 201, first.text
        second = await client.post(f"/api/v1/jobs/{job.id}/apply", json={})
    assert second.status_code == 409, second.text


async def test_apply_route_returns_400_without_a_profile(database) -> None:
    job = await _seed(database, with_profile=False)
    async with _http(database) as client:
        response = await client.post(f"/api/v1/jobs/{job.id}/apply", json={})
    assert response.status_code == 400, response.text
