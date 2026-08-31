"""API smoke tests using FastAPI's TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient

from applyuminati.api.app import create_app
from applyuminati.core.settings import SecuritySettings
from applyuminati.db.session import set_database
from applyuminati.services.container import set_container


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


def test_settings_endpoint(database) -> None:
    client = _client(database)
    r = client.get("/api/v1/settings")
    assert r.status_code == 200
    assert r.json()["execution_mode"] == "research_only"
