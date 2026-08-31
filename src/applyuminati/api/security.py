"""Request authentication, CSRF protection and the auth endpoints.

Enforcement is middleware rather than a per-route dependency, on purpose: a
dependency has to be remembered on every new router, and the one that gets
forgotten is the one that leaks a resume. Middleware fails closed, so a route
added tomorrow is protected by default and has to opt *out* explicitly through
:data:`PUBLIC_PATHS`.

Two credentials reach this module and they are not interchangeable:

* A **session cookie**, set by ``POST /api/v1/auth/login``. Because a browser
  attaches it automatically, cookie-authenticated state-changing requests must
  also pass a double-submit CSRF check and an origin check.
* A **bearer token**, the same session token in an ``Authorization`` header, for
  scripts. A browser never adds that header on its own, so there is no
  cross-site request to forge and CSRF does not apply.

Browser Hosts do not authenticate here. They present their own credential on
their own endpoint (see :mod:`applyuminati.api.routers.browser_hosts`), which
keeps a machine credential from ever being usable against the human API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from applyuminati.api.dependencies import get_settings
from applyuminati.core.clock import utcnow
from applyuminati.core.errors import FailureCategory
from applyuminati.core.logging import get_logger
from applyuminati.core.security import (
    SessionFailure,
    csrf_matches,
    issue_session,
    verify_configured_password,
    verify_session,
)
from applyuminati.core.settings import Settings

log = get_logger(__name__)

#: Paths reachable without a session.
#:
#: ``/api/v1/health`` stays open so a container healthcheck, a reverse proxy and
#: Portainer keep working without holding a credential; it reports liveness and
#: backend availability, never user data. The auth endpoints are open because
#: they are how a session is obtained in the first place.
PUBLIC_API_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/health",
        "/api/v1/auth/session",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
    }
)

_UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_API_PREFIX = "/api/"


class AuthStatus(BaseModel):
    """What the UI needs to decide between the app and a login form."""

    model_config = ConfigDict(extra="forbid")

    #: False when authentication is switched off entirely.
    required: bool
    #: False when no password has been set yet, so the UI can say so.
    configured: bool
    authenticated: bool
    csrf_token: str | None = None
    expires_at: int | None = None
    #: True when the server is reachable from other machines, so the UI can warn.
    listens_beyond_loopback: bool = False


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1)


#: HTTP status to the domain failure category, so an auth rejection reaching the
#: UI carries the same taxonomy as any other failure rather than a special case.
_REJECTION_CATEGORIES: dict[int, FailureCategory] = {
    401: FailureCategory.AUTH_REQUIRED,
    403: FailureCategory.AUTH_REQUIRED,
    429: FailureCategory.RATE_LIMITED,
    503: FailureCategory.CONFIGURATION,
}


def _unauthorised(code: str, message: str, *, status: int = 401) -> JSONResponse:
    """Error envelope matching :class:`applyuminati.api.schemas.ErrorResponse`."""
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "category": _REJECTION_CATEGORIES[status].value,
            "message": message,
            "recovery": "escalate_to_user",
            "retryable": status == 429,
            "details": {},
        },
    )


def _is_public(path: str) -> bool:
    if path in PUBLIC_API_PATHS:
        return True
    # Everything outside /api is the SPA shell or a static file, which has to
    # load before anyone can sign in.
    return not path.startswith(_API_PREFIX)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _origin_allowed(request: Request, settings: Settings) -> bool:
    """Same-origin check for cookie-authenticated state changes.

    A browser sends ``Origin`` on every cross-site request and on same-origin
    unsafe requests, so an absent header means a non-browser client, which
    cannot be a cross-site forgery. ``Referer`` is the fallback for the rare
    browser that omits ``Origin``.
    """
    raw = request.headers.get("Origin") or request.headers.get("Referer")
    if not raw:
        return True
    if raw in settings.security.trusted_origins:
        return True
    parsed = urlsplit(raw)
    host_header = request.headers.get("Host", "")
    return bool(parsed.netloc) and parsed.netloc == host_header


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Fail-closed session enforcement for everything under ``/api``."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._settings.security.enabled or _is_public(request.url.path):
            return await call_next(request)
        rejection = self._reject(request)
        return rejection if rejection is not None else await call_next(request)

    def _reject(self, request: Request) -> JSONResponse | None:
        """Return the rejection response, or ``None`` when the request may pass."""
        security = self._settings.security
        material = security.session_key_material()
        if material is None:
            return _unauthorised(
                "auth.not_configured",
                "no password is set. Set APPLYUMINATI_SECURITY__PASSWORD (or "
                "security.password in config.toml) and restart.",
                status=503,
            )

        token = _bearer_token(request)
        from_cookie = token is None
        if token is None:
            token = request.cookies.get(security.session_cookie_name)
        if not token:
            return _unauthorised("auth.required", "sign in to use this API")

        session = verify_session(token, material, now=int(utcnow().timestamp()))
        if isinstance(session, SessionFailure):
            return _unauthorised(f"auth.session_{session.value}", "session is not valid")

        if from_cookie and request.method in _UNSAFE_METHODS:
            return self._check_cross_site(request, session.csrf_token(material))
        return None

    def _check_cross_site(self, request: Request, expected_csrf: str) -> JSONResponse | None:
        """Origin plus double-submit check for cookie-authenticated writes."""
        security = self._settings.security
        if not _origin_allowed(request, self._settings):
            return _unauthorised(
                "auth.origin_rejected",
                "request origin does not match this server",
                status=403,
            )
        if not csrf_matches(request.headers.get(security.csrf_header_name), expected_csrf):
            return _unauthorised(
                "auth.csrf_failed",
                f"missing or stale {security.csrf_header_name} header",
                status=403,
            )
        return None


class LoginThrottle:
    """Per-client backoff on failed logins.

    One password guarded by a KDF is not much of a defence if a caller on the
    LAN can try ten thousand guesses a minute. Failures are counted per client
    address and, past :attr:`free_attempts`, each one buys a doubling lockout up
    to :attr:`max_lockout_seconds`. A success clears the counter.

    Counting per client rather than globally is deliberate: a global lockout
    would let anyone on the network lock the operator out of their own machine.
    An attacker who can rotate source addresses defeats this, which is why the
    KDF cost and the minimum password length matter independently.
    """

    free_attempts = 5
    base_lockout_seconds = 2
    max_lockout_seconds = 300
    #: Cap on tracked clients, so a spray across forged addresses cannot grow
    #: this without bound. The oldest entry is evicted.
    max_tracked = 1024

    def __init__(self) -> None:
        self._failures: dict[str, tuple[int, float]] = {}

    def retry_after(self, client: str, *, now: float) -> int:
        """Seconds the client must wait, or 0 when it may attempt a login."""
        entry = self._failures.get(client)
        if entry is None:
            return 0
        _, locked_until = entry
        return max(0, int(locked_until - now) + 1) if locked_until > now else 0

    def record_failure(self, client: str, *, now: float) -> None:
        count = self._failures.get(client, (0, 0.0))[0] + 1
        penalty = 0.0
        if count > self.free_attempts:
            exponent = count - self.free_attempts - 1
            penalty = min(
                self.base_lockout_seconds * (2**exponent),
                self.max_lockout_seconds,
            )
        if client not in self._failures and len(self._failures) >= self.max_tracked:
            self._failures.pop(next(iter(self._failures)))
        self._failures[client] = (count, now + penalty)

    def record_success(self, client: str) -> None:
        self._failures.pop(client, None)


#: Process-wide, because the throttle is only meaningful across requests and this
#: deployment is a single process. It is state that must not survive a restart.
_login_throttle = LoginThrottle()


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _status(request: Request, settings: Settings, *, token: str | None = None) -> AuthStatus:
    security = settings.security
    status = AuthStatus(
        required=security.enabled,
        configured=security.configured,
        authenticated=not security.enabled,
        listens_beyond_loopback=settings.listens_beyond_loopback,
    )
    material = security.session_key_material()
    if not security.enabled or material is None:
        return status
    presented = token or request.cookies.get(security.session_cookie_name)
    if not presented:
        return status
    session = verify_session(presented, material, now=int(utcnow().timestamp()))
    if isinstance(session, SessionFailure):
        return status
    status.authenticated = True
    status.csrf_token = session.csrf_token(material)
    status.expires_at = session.expires_at
    return status


@router.get("/session", response_model=AuthStatus)
async def read_session(request: Request, settings: Settings = Depends(get_settings)) -> AuthStatus:
    """Whether this client is signed in. Never reveals whether a guess was close."""
    return _status(request, settings)


@router.post("/login", response_model=AuthStatus)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> AuthStatus | JSONResponse:
    security = settings.security
    if not security.enabled:
        return _status(request, settings)
    configured = security.configured_secret()
    material = security.session_key_material()
    if configured is None or material is None:
        return _unauthorised(
            "auth.not_configured",
            "no password is set on this instance",
            status=503,
        )

    client = request.client.host if request.client else "unknown"
    now = int(utcnow().timestamp())
    wait = _login_throttle.retry_after(client, now=now)
    if wait:
        log.warning("auth.login_throttled", client=client, retry_after=wait)
        rejection = _unauthorised(
            "auth.too_many_attempts",
            f"too many failed attempts; try again in {wait}s",
            status=429,
        )
        # Set on the returned response, not the injected one: returning a
        # Response directly bypasses FastAPI's header merging.
        rejection.headers["Retry-After"] = str(wait)
        return rejection
    if not verify_configured_password(payload.password, configured):
        _login_throttle.record_failure(client, now=now)
        log.warning("auth.login_failed", client=client)
        return _unauthorised("auth.invalid_password", "incorrect password")
    _login_throttle.record_success(client)

    token = issue_session(material, now=now, ttl_seconds=security.session_ttl_seconds)
    status = _status(request, settings, token=token)
    response.set_cookie(
        security.session_cookie_name,
        token,
        max_age=security.session_ttl_seconds,
        httponly=True,
        samesite="strict",
        secure=security.https_only,
        path="/",
    )
    if status.csrf_token:
        # Readable by the SPA on purpose: the double-submit check needs the
        # value in a header, which script has to be able to set.
        response.set_cookie(
            security.csrf_cookie_name,
            status.csrf_token,
            max_age=security.session_ttl_seconds,
            httponly=False,
            samesite="strict",
            secure=security.https_only,
            path="/",
        )
    log.info("auth.login", ttl_hours=security.session_ttl_hours)
    return status


@router.post("/logout", response_model=AuthStatus)
async def logout(
    request: Request, response: Response, settings: Settings = Depends(get_settings)
) -> AuthStatus:
    security = settings.security
    response.delete_cookie(security.session_cookie_name, path="/")
    response.delete_cookie(security.csrf_cookie_name, path="/")
    return AuthStatus(
        required=security.enabled,
        configured=security.configured,
        authenticated=not security.enabled,
        listens_beyond_loopback=settings.listens_beyond_loopback,
    )


__all__ = [
    "PUBLIC_API_PATHS",
    "AuthStatus",
    "AuthenticationMiddleware",
    "LoginRequest",
    "LoginThrottle",
    "router",
]
