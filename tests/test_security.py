"""Authentication, CSRF and exposure guards.

These are the tests that matter most in this file: a regression here does not
break a feature, it publishes a resume, a home address and a salary
expectation to anyone on the network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from applyuminati.api import security as api_security
from applyuminati.api.app import create_app
from applyuminati.api.security import LoginThrottle
from applyuminati.core.security import (
    SessionFailure,
    csrf_matches,
    hash_password,
    is_password_hash,
    issue_session,
    mint_host_credential,
    session_material,
    verify_configured_password,
    verify_host_credential,
    verify_password,
    verify_session,
)
from applyuminati.core.settings import SecuritySettings, ServerSettings, Settings
from applyuminati.db.session import set_database
from applyuminati.services.container import set_container

PASSWORD = "correct-horse-battery"
CSRF_HEADER = "X-Applyuminati-CSRF"


@pytest.fixture(autouse=True)
def _fresh_throttle():
    """The throttle is process-wide, so failures must not leak between tests."""
    api_security._login_throttle = LoginThrottle()


def _client(database, *, https_only: bool = False) -> TestClient:
    set_container(None)
    set_database(database)
    settings = database.settings.model_copy(
        update={
            "security": SecuritySettings(
                enabled=True, password=SecretStr(PASSWORD), https_only=https_only
            )
        }
    )
    return TestClient(create_app(settings))


def _settings(tmp_path, *, host: str, security: SecuritySettings) -> Settings:
    return Settings(data_dir=tmp_path, server=ServerSettings(host=host), security=security)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def test_password_hash_roundtrip() -> None:
    # Reduced iterations: the test asserts the algorithm, not the cost factor.
    encoded = hash_password(PASSWORD, iterations=1000)
    assert is_password_hash(encoded)
    assert verify_password(PASSWORD, encoded)
    assert not verify_password(PASSWORD + "x", encoded)


def test_password_hash_is_salted() -> None:
    assert hash_password(PASSWORD, iterations=1000) != hash_password(PASSWORD, iterations=1000)


def test_short_password_is_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        hash_password("short")


def test_malformed_hash_does_not_verify() -> None:
    for broken in ("", "nonsense", "pbkdf2_sha256$notanint$a$b", "pbkdf2_sha256$1000$!!$??"):
        assert not verify_password(PASSWORD, broken)


def test_configured_password_accepts_plaintext_or_hash() -> None:
    assert verify_configured_password(PASSWORD, PASSWORD)
    assert not verify_configured_password("wrong", PASSWORD)
    assert verify_configured_password(PASSWORD, hash_password(PASSWORD, iterations=1000))
    assert not verify_configured_password(PASSWORD, "")


def test_session_material_is_deterministic() -> None:
    """A per-call salt here would invalidate every session on every request."""
    assert session_material(PASSWORD) == session_material(PASSWORD)
    assert session_material(PASSWORD) != session_material(PASSWORD + "x")


def test_session_roundtrip_and_expiry() -> None:
    material = session_material(PASSWORD)
    token = issue_session(material, now=1000, ttl_seconds=60)
    session = verify_session(token, material, now=1010)
    assert not isinstance(session, SessionFailure)
    assert session.expires_at == 1060
    assert verify_session(token, material, now=1060) is SessionFailure.EXPIRED
    assert verify_session(token, material, now=2000) is SessionFailure.EXPIRED


def test_session_rejects_tampering_and_a_changed_password() -> None:
    material = session_material(PASSWORD)
    token = issue_session(material, now=1000, ttl_seconds=60)
    head, _, signature = token.rpartition(".")
    assert verify_session(f"{head}.{signature[:-2]}xy", material, now=1010) in (
        SessionFailure.BAD_SIGNATURE,
        SessionFailure.MALFORMED,
    )
    issued, expires, nonce, sig = token.split(".")
    forged = f"{issued}.{int(expires) + 99999}.{nonce}.{sig}"
    assert verify_session(forged, material, now=1010) is SessionFailure.BAD_SIGNATURE
    # Changing the password invalidates outstanding sessions with no revocation
    # list to maintain.
    assert isinstance(
        verify_session(token, session_material("new-password"), now=1010), SessionFailure
    )


def test_session_rejects_garbage() -> None:
    material = session_material(PASSWORD)
    assert verify_session("", material, now=1) is SessionFailure.MALFORMED
    assert verify_session("a.b.c", material, now=1) is SessionFailure.MALFORMED
    assert verify_session("a.b.c.d", material, now=1) is SessionFailure.MALFORMED


def test_csrf_token_is_bound_to_the_session() -> None:
    material = session_material(PASSWORD)
    first = verify_session(issue_session(material, now=1, ttl_seconds=60), material, now=2)
    second = verify_session(issue_session(material, now=1, ttl_seconds=60), material, now=2)
    assert not isinstance(first, SessionFailure)
    assert not isinstance(second, SessionFailure)
    assert first.csrf_token(material) != second.csrf_token(material)
    assert csrf_matches(first.csrf_token(material), first.csrf_token(material))
    assert not csrf_matches(first.csrf_token(material), second.csrf_token(material))
    assert not csrf_matches(None, first.csrf_token(material))
    assert not csrf_matches("", "")


def test_host_credential_stores_only_a_hash() -> None:
    minted = mint_host_credential()
    assert len(minted.secret) >= 40
    assert minted.secret not in minted.hashed
    assert minted.prefix
    assert minted.secret.startswith(minted.prefix)
    assert verify_host_credential(minted.secret, minted.hashed)
    assert not verify_host_credential(minted.secret + "x", minted.hashed)


# ---------------------------------------------------------------------------
# Exposure guard
# ---------------------------------------------------------------------------


#: Any interface, which is what makes an open API a network service.
EXPOSED = "0.0.0.0"  # noqa: S104 - asserted against, never bound in a test


def test_unauthenticated_api_on_a_network_interface_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="refusing to serve an unauthenticated API"):
        _settings(tmp_path, host=EXPOSED, security=SecuritySettings(enabled=False))


def test_unauthenticated_api_on_loopback_is_allowed(tmp_path) -> None:
    settings = _settings(tmp_path, host="127.0.0.1", security=SecuritySettings(enabled=False))
    assert not settings.listens_beyond_loopback


def test_exposure_can_be_allowed_deliberately(tmp_path) -> None:
    settings = _settings(
        tmp_path,
        host=EXPOSED,
        security=SecuritySettings(enabled=False, allow_unauthenticated_exposure=True),
    )
    assert settings.listens_beyond_loopback


def test_authenticated_api_may_bind_a_network_interface(tmp_path) -> None:
    settings = _settings(
        tmp_path, host=EXPOSED, security=SecuritySettings(password=SecretStr(PASSWORD))
    )
    assert settings.security.configured


def test_password_never_appears_in_public_settings(tmp_path) -> None:
    settings = _settings(
        tmp_path, host="127.0.0.1", security=SecuritySettings(password=SecretStr(PASSWORD))
    )
    payload = settings.public_dict()
    assert PASSWORD not in repr(payload)
    assert payload["security"]["configured"] is True
    assert "password" not in payload["security"]


# ---------------------------------------------------------------------------
# Request enforcement
# ---------------------------------------------------------------------------


def test_unauthenticated_requests_are_rejected(database) -> None:
    client = _client(database)
    for path in ("/api/v1/jobs", "/api/v1/profile", "/api/v1/dashboard", "/api/v1/settings"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["code"] == "auth.required"


def test_health_and_session_stay_public(database) -> None:
    client = _client(database)
    assert client.get("/api/v1/health").status_code == 200
    body = client.get("/api/v1/auth/session").json()
    assert body == {
        "required": True,
        "configured": True,
        "authenticated": False,
        "csrf_token": None,
        "expires_at": None,
        "listens_beyond_loopback": False,
    }


def test_login_then_read(database) -> None:
    client = _client(database)
    assert client.post("/api/v1/auth/login", json={"password": "wrong"}).status_code == 401
    response = client.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["csrf_token"]
    assert client.get("/api/v1/jobs").status_code == 200


def test_session_cookie_is_httponly_and_samesite(database) -> None:
    client = _client(database)
    response = client.post("/api/v1/auth/login", json={"password": PASSWORD})
    header = response.headers["set-cookie"]
    assert "applyuminati_session" in header
    assert "HttpOnly" in header
    assert "strict" in header.lower()


def test_https_only_adds_the_secure_attribute(database) -> None:
    client = _client(database, https_only=True)
    response = client.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert "Secure" in response.headers["set-cookie"]


def test_cookie_authenticated_writes_need_the_csrf_header(database) -> None:
    client = _client(database)
    client.post("/api/v1/auth/login", json={"password": PASSWORD})
    # A cross-site form post carries the cookie but cannot read the CSRF value.
    forged = client.post("/api/v1/jobs/discover", json={})
    assert forged.status_code == 403
    assert forged.json()["code"] == "auth.csrf_failed"


def test_csrf_header_from_the_cookie_is_accepted(database) -> None:
    client = _client(database)
    client.post("/api/v1/auth/login", json={"password": PASSWORD})
    token = client.cookies.get("applyuminati_csrf")
    assert token
    allowed = client.post("/api/v1/jobs/discover", json={}, headers={CSRF_HEADER: token})
    assert allowed.status_code != 403


def test_a_stale_csrf_token_is_rejected(database) -> None:
    client = _client(database)
    client.post("/api/v1/auth/login", json={"password": PASSWORD})
    stale = "not-the-right-token"
    assert (
        client.post("/api/v1/jobs/discover", json={}, headers={CSRF_HEADER: stale}).status_code
        == 403
    )


def test_bearer_token_skips_csrf(database) -> None:
    """A browser never sets Authorization itself, so there is nothing to forge."""
    client = _client(database)
    token = client.post("/api/v1/auth/login", json={"password": PASSWORD}).json()
    assert token["authenticated"]
    session_cookie = client.cookies.get("applyuminati_session")
    assert session_cookie
    client.cookies.clear()
    response = client.post(
        "/api/v1/jobs/discover", json={}, headers={"Authorization": f"Bearer {session_cookie}"}
    )
    assert response.status_code != 401
    assert response.status_code != 403


def test_cross_origin_write_is_rejected(database) -> None:
    client = _client(database)
    client.post("/api/v1/auth/login", json={"password": PASSWORD})
    token = client.cookies.get("applyuminati_csrf") or ""
    response = client.post(
        "/api/v1/jobs/discover",
        json={},
        headers={CSRF_HEADER: token, "Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "auth.origin_rejected"


def test_logout_clears_the_session(database) -> None:
    client = _client(database)
    client.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert client.get("/api/v1/jobs").status_code == 200
    client.post(
        "/api/v1/auth/logout",
        headers={CSRF_HEADER: client.cookies.get("applyuminati_csrf") or ""},
    )
    assert client.get("/api/v1/jobs").status_code == 401


# ---------------------------------------------------------------------------
# Brute-force throttle
# ---------------------------------------------------------------------------


def test_throttle_allows_a_few_attempts_then_locks() -> None:
    throttle = LoginThrottle()
    for _ in range(throttle.free_attempts):
        assert throttle.retry_after("10.0.0.1", now=0.0) == 0
        throttle.record_failure("10.0.0.1", now=0.0)
    throttle.record_failure("10.0.0.1", now=0.0)
    assert throttle.retry_after("10.0.0.1", now=0.0) > 0


def test_throttle_backoff_grows_and_is_capped() -> None:
    throttle = LoginThrottle()
    for _ in range(40):
        throttle.record_failure("10.0.0.1", now=0.0)
    assert throttle.retry_after("10.0.0.1", now=0.0) <= throttle.max_lockout_seconds + 1


def test_throttle_is_per_client_so_nobody_can_lock_the_operator_out() -> None:
    throttle = LoginThrottle()
    for _ in range(20):
        throttle.record_failure("10.0.0.9", now=0.0)
    assert throttle.retry_after("10.0.0.9", now=0.0) > 0
    assert throttle.retry_after("127.0.0.1", now=0.0) == 0


def test_throttle_lockout_expires_and_success_clears_it() -> None:
    throttle = LoginThrottle()
    for _ in range(6):
        throttle.record_failure("10.0.0.1", now=0.0)
    assert throttle.retry_after("10.0.0.1", now=1000.0) == 0
    throttle.record_success("10.0.0.1")
    for _ in range(throttle.free_attempts):
        throttle.record_failure("10.0.0.1", now=0.0)
    assert throttle.retry_after("10.0.0.1", now=0.0) == 0


def test_throttle_does_not_grow_without_bound() -> None:
    throttle = LoginThrottle()
    for index in range(throttle.max_tracked * 2):
        throttle.record_failure(f"10.1.{index // 256}.{index % 256}", now=0.0)
    assert len(throttle._failures) <= throttle.max_tracked


def test_repeated_bad_logins_are_throttled_over_http(database) -> None:
    client = _client(database)
    codes = [
        client.post("/api/v1/auth/login", json={"password": "wrong"}).status_code
        for _ in range(LoginThrottle.free_attempts + 2)
    ]
    assert codes[0] == 401
    assert codes[-1] == 429
    throttled = client.post("/api/v1/auth/login", json={"password": "wrong"})
    assert throttled.headers["Retry-After"]
    assert throttled.json()["retryable"] is True
    # The correct password is refused too while locked out, so a guess cannot be
    # confirmed by racing the lockout.
    assert client.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 429


def test_api_without_a_password_refuses_rather_than_opening(database) -> None:
    set_container(None)
    set_database(database)
    settings = database.settings.model_copy(
        update={"security": SecuritySettings(enabled=True, password=None)}
    )
    client = TestClient(create_app(settings))
    response = client.get("/api/v1/jobs")
    assert response.status_code == 503
    assert response.json()["code"] == "auth.not_configured"
    assert client.get("/api/v1/health").status_code == 200
