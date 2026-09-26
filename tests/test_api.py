"""API smoke tests using FastAPI's TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient

from applyuminati.api.app import create_app
from applyuminati.core.models.application import Application, ApplicationState
from applyuminati.core.models.job import SourceTier
from applyuminati.core.models.jsonresume import JsonResume, ResumeBasics
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.settings import SecuritySettings
from applyuminati.db.models import ProfileRow
from applyuminati.db.repositories.jobs import JobRepository
from applyuminati.db.session import set_database
from applyuminati.services.container import Repositories, set_container
from applyuminati.sources.normalize import build_job


def _client(database, **security):
    # Force a fresh ServiceContainer bound to this test's database: both are
    # process-wide singletons that otherwise survive across tests in the same
    # pytest process.
    #
    # Authentication is off by default here so these tests stay about routing
    # and payloads. tests/test_security.py is where it is turned on. The bind
    # address stays loopback, which is the only configuration where Settings
    # permits an unauthenticated API at all.
    set_container(None)
    set_database(database)
    app = create_app(
        database.settings.model_copy(
            update={"security": SecuritySettings(enabled=False, **security)}
        )
    )
    return TestClient(app)


def test_health_endpoint(database) -> None:
    client = _client(database)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database_ok"] is True


def test_sources_list(database) -> None:
    client = _client(database)
    r = client.get("/api/v1/sources")
    assert r.status_code == 200
    sources = r.json()
    slugs = {s["slug"] for s in sources}
    assert {"greenhouse", "lever", "local_feed"} <= slugs


def test_profile_import_and_get(database, sample_resume) -> None:
    client = _client(database)
    r = client.post("/api/v1/profile/import", json={"resume": sample_resume, "replace": True})
    assert r.status_code == 200
    assert r.json()["claims_created"] > 0
    r2 = client.get("/api/v1/profile")
    assert r2.status_code == 200
    assert r2.json()["name"] == "Jane Engineer"


def test_dashboard_endpoint(database) -> None:
    client = _client(database)
    r = client.get("/api/v1/dashboard")
    assert r.status_code == 200
    assert r.json()["total_jobs"] == 0


def test_jobs_list_empty(database) -> None:
    client = _client(database)
    r = client.get("/api/v1/jobs")
    assert r.status_code == 200
    assert r.json()["total"] == 0


async def _seed_job_with_application(
    database, *, slug: str, state: ApplicationState | None, profile_id: str = "p1"
) -> str:
    """Persist one job, optionally with the user's application for it."""
    job = build_job(
        source="local_feed",
        tier=SourceTier.AGGREGATOR,
        source_job_id=slug,
        url=f"https://example.com/jobs/{slug}",
        title=f"Engineer {slug}",
        company=f"Company {slug}",
        apply_url=f"https://example.com/jobs/{slug}/apply",
    )
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        await repos.profiles.upsert(
            CareerProfile(
                id=profile_id, label=profile_id, resume=JsonResume(basics=ResumeBasics(name="T"))
            )
        )
        if state is not None:
            await repos.applications.save(
                Application(job_id=job.id, profile_id=profile_id, state=state)
            )
    return job.id


async def test_jobs_list_filters_by_application_state(database) -> None:
    """`?state=` must narrow the list, not be accepted and ignored."""
    shortlisted = await _seed_job_with_application(
        database, slug="a", state=ApplicationState.SHORTLISTED
    )
    await _seed_job_with_application(database, slug="b", state=ApplicationState.REJECTED)
    await _seed_job_with_application(database, slug="c", state=None)

    client = _client(database)
    unfiltered = client.get("/api/v1/jobs")
    assert unfiltered.status_code == 200
    assert unfiltered.json()["total"] == 3

    r = client.get("/api/v1/jobs", params={"state": ApplicationState.SHORTLISTED.value})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [shortlisted]
    assert body["items"][0]["application_state"] == ApplicationState.SHORTLISTED.value


async def test_jobs_list_state_filter_ignores_another_profile(database) -> None:
    """A state filter must resolve against the active profile only."""
    mine = await _seed_job_with_application(
        database, slug="mine", state=ApplicationState.SHORTLISTED
    )
    await _seed_job_with_application(
        database, slug="theirs", state=ApplicationState.SHORTLISTED, profile_id="p2"
    )
    # There is one active profile, so the other is switched off at the row
    # level: which of two active profiles the API picks is not defined.
    async with database.session() as session:
        other = await session.get(ProfileRow, "p2")
        assert other is not None
        other.is_active = False

    client = _client(database)
    r = client.get("/api/v1/jobs", params={"state": ApplicationState.SHORTLISTED.value})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [mine]


def test_jobs_list_rejects_an_unknown_state(database) -> None:
    client = _client(database)
    r = client.get("/api/v1/jobs", params={"state": "not_a_state"})
    assert 400 <= r.status_code < 500, r.text


def test_settings_endpoint(database) -> None:
    client = _client(database)
    r = client.get("/api/v1/settings")
    assert r.status_code == 200
    assert r.json()["execution_mode"] == "research_only"
