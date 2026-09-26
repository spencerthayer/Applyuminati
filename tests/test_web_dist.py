"""Static bundle mounting: auto-detection, degradation, and SPA deep links."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from applyuminati.api.app import create_app, resolve_web_dist
from applyuminati.core.settings import SecuritySettings
from applyuminati.db.session import set_database
from applyuminati.services.container import set_container

INDEX_HTML = "<!doctype html><title>Applyuminati</title>"


def _client(database, web_dist: Path | None) -> TestClient:
    set_container(None)
    set_database(database)
    app = create_app(
        database.settings.model_copy(
            update={
                "security": SecuritySettings(enabled=False),
                "server": database.settings.server.model_copy(update={"web_dist": web_dist}),
            }
        )
    )
    return TestClient(app)


def _build_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "index.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_root_serves_the_configured_bundle(database, tmp_path) -> None:
    dist = _build_dist(tmp_path)
    r = _client(database, dist).get("/")
    assert r.status_code == 200
    assert INDEX_HTML in r.text


def test_deep_link_falls_back_to_index(database, tmp_path) -> None:
    """A client-side route like /jobs/abc is a real URL, so it must not 404."""
    dist = _build_dist(tmp_path)
    r = _client(database, dist).get("/jobs/abc")
    assert r.status_code == 200
    assert INDEX_HTML in r.text


def test_unknown_api_path_is_404_not_the_spa(database, tmp_path) -> None:
    """The SPA catch-all must not swallow API 404s into 200 text/html."""
    client = _client(database, _build_dist(tmp_path))
    r = client.get("/api/v1/nonexistent-thing")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_real_asset_is_served_before_the_index(database, tmp_path) -> None:
    dist = _build_dist(tmp_path)
    r = _client(database, dist).get("/assets/index.js")
    assert r.status_code == 200
    assert "console.log(1)" in r.text


def test_missing_dist_leaves_the_api_serving(database, tmp_path) -> None:
    client = _client(database, tmp_path / "not-built")
    assert client.get("/api/v1/health").status_code == 200


def test_resolver_finds_nothing_in_an_empty_tree(tmp_path) -> None:
    package_dir = tmp_path / "site-packages" / "applyuminati"
    package_dir.mkdir(parents=True)
    assert resolve_web_dist(None, package_dir) is None


def test_resolver_prefers_the_configured_path(tmp_path) -> None:
    configured = tmp_path / "elsewhere"
    package_dir = tmp_path / "site-packages" / "applyuminati"
    package_dir.mkdir(parents=True)
    _build_dist(tmp_path / "repo")
    # A configured path is honoured verbatim, existing or not: the caller logs
    # the miss rather than silently serving some other bundle.
    assert resolve_web_dist(configured, package_dir) == configured


def test_resolver_finds_a_checkout_layout(tmp_path) -> None:
    repo = tmp_path / "repo"
    expected = _build_dist(repo / "apps" / "web")
    package_dir = repo / "src" / "applyuminati"
    package_dir.mkdir(parents=True)
    assert resolve_web_dist(None, package_dir) == expected


def test_resolver_falls_back_to_packaged_data(tmp_path) -> None:
    package_dir = tmp_path / "site-packages" / "applyuminati"
    packaged = package_dir / "web_dist"
    packaged.mkdir(parents=True)
    assert resolve_web_dist(None, package_dir) == packaged
