"""API smoke tests using FastAPI's TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient

from applyuminati.api.app import create_app
from applyuminati.db.session import set_database


def _client(database):
    set_database(database)
    app = create_app()
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
