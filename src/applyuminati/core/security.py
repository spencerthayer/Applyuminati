"""Password hashing, session tokens and CSRF tokens.

"Local-first" does not make an unauthenticated service safe. Applyuminati holds
names, addresses, phone numbers, employment history, resumes, salary
expectations, work-authorisation status, application answers, recruiter mail and
browser activity. Anything listening on a LAN-reachable interface needs a lock
on it.

The model here is deliberately small. There is no user table, no OAuth, no
identity provider: one operator, one password, signed stateless sessions.

Three properties are load-bearing:

* **Stdlib only.** `hashlib`, `hmac`, `secrets` and `base64`. No new
  dependency, nothing to keep patched, and this module stays importable from
  `core` without breaking the vendor-neutrality contract.
* **Sessions are derived from the password hash.** The signing key is an HMAC of
  the stored hash, so changing the password invalidates every existing session
  with no revocation list to maintain, and no secret to configure separately.
* **Constant-time comparison everywhere.** Password verification, session
  signature checks and CSRF checks all use `hmac.compare_digest`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from enum import StrEnum

#: PBKDF2 cost. Chosen so a login costs a few hundred milliseconds on the kind
#: of hardware this runs on (a laptop, a NAS, a Raspberry Pi) while a
#: dictionary attack against a leaked hash stays expensive.
PBKDF2_ITERATIONS = 600_000
PBKDF2_ALGORITHM = "sha256"
_SCHEME = "pbkdf2_sha256"
_SALT_BYTES = 16
_MIN_PASSWORD_LENGTH = 10

#: Domain separation for every derived key, so a session token can never be
#: replayed as a CSRF token or vice versa.
_SESSION_KEY_INFO = b"applyuminati.session.v1"
_CSRF_KEY_INFO = b"applyuminati.csrf.v1"

#: Length of the credential minted for a Browser Host.
HOST_CREDENTIAL_BYTES = 32
#: Non-secret leading characters kept for display ("token abc123... revoked").
HOST_CREDENTIAL_PREFIX_LENGTH = 8


class SessionFailure(StrEnum):
    """Why a session token was rejected. Reported as a code, never as prose."""

    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


class WeakPasswordError(ValueError):
    """Raised when a password is too short to be worth hashing."""


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return an encoded PBKDF2 hash: ``pbkdf2_sha256$iters$salt$hash``.

    Self-describing so the iteration count can be raised later without
    invalidating existing hashes.
    """
    if len(password) < _MIN_PASSWORD_LENGTH:
        msg = f"password must be at least {_MIN_PASSWORD_LENGTH} characters"
        raise WeakPasswordError(msg)
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode("utf-8"), salt, iterations)
    return f"{_SCHEME}${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification. A malformed hash verifies as ``False``."""
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$")
        if scheme != _SCHEME:
            return False
        iterations = int(raw_iterations)
        salt = _b64d(raw_salt)
        expected = _b64d(raw_digest)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def is_password_hash(value: str) -> bool:
    """True when a configured value is already an encoded hash.

    Lets one setting accept either a plaintext password (convenient in a compose
    file) or a hash (correct on a shared host) without a second setting.
    """
    return value.startswith(f"{_SCHEME}$") and value.count("$") == 3


def verify_configured_password(candidate: str, configured: str) -> bool:
    """Verify a login against whichever form the operator configured.

    When ``configured`` is an encoded hash, this is PBKDF2. When it is a
    plaintext password it is a constant-time comparison, because running a
    600k-iteration KDF over a secret that is already sitting in the process
    environment protects nothing and only makes login slow.
    """
    if is_password_hash(configured):
        return verify_password(candidate, configured)
    if not configured:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), configured.encode("utf-8"))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def session_material(configured_password: str) -> str:
    """Deterministic signing material derived from the configured password.

    Deterministic matters twice over. A salted hash recomputed per request would
    produce a different signing key each time and invalidate every session
    instantly, and a key regenerated on boot would log the operator out on every
    restart. Deriving from the configured value keeps sessions valid across
    restarts and invalidates them all the moment the password changes.
    """
    return hashlib.sha256(
        b"applyuminati.session.material.v1" + configured_password.encode("utf-8")
    ).hexdigest()


def _derive_key(material: str, info: bytes) -> bytes:
    return hmac.new(material.encode("utf-8"), info, hashlib.sha256).digest()


@dataclass(frozen=True, slots=True)
class SessionToken:
    """A verified session.

    ``issued_at`` and ``expires_at`` are epoch seconds; ``nonce`` makes two
    tokens issued in the same second distinct, so a CSRF token derived from the
    session is not shared between logins.
    """

    issued_at: int
    expires_at: int
    nonce: str

    def csrf_token(self, material: str) -> str:
        """CSRF token bound to this session.

        Derived rather than stored, so the double-submit check needs no server
        state and a stolen CSRF token is useless with a different session.
        """
        key = _derive_key(material, _CSRF_KEY_INFO)
        return _b64e(hmac.new(key, self.nonce.encode("ascii"), hashlib.sha256).digest())


def issue_session(material: str, *, now: int, ttl_seconds: int) -> str:
    """Mint a signed session token. Opaque to the client."""
    nonce = secrets.token_urlsafe(12)
    expires_at = now + ttl_seconds
    payload = f"{now}.{expires_at}.{nonce}"
    key = _derive_key(material, _SESSION_KEY_INFO)
    signature = hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64e(signature)}"


def verify_session(token: str, material: str, *, now: int) -> SessionToken | SessionFailure:
    """Verify and decode a session token.

    Returns the decoded token or a :class:`SessionFailure`, rather than raising:
    a rejected session is an expected condition on every unauthenticated
    request, not an exceptional one.
    """
    parts = token.split(".")
    if len(parts) != 4:
        return SessionFailure.MALFORMED
    raw_issued, raw_expires, nonce, raw_signature = parts
    try:
        issued_at = int(raw_issued)
        expires_at = int(raw_expires)
        signature = _b64d(raw_signature)
    except (ValueError, TypeError):
        return SessionFailure.MALFORMED
    key = _derive_key(material, _SESSION_KEY_INFO)
    expected = hmac.new(key, f"{raw_issued}.{raw_expires}.{nonce}".encode("ascii"), hashlib.sha256)
    if not hmac.compare_digest(signature, expected.digest()):
        return SessionFailure.BAD_SIGNATURE
    if now >= expires_at:
        return SessionFailure.EXPIRED
    return SessionToken(issued_at=issued_at, expires_at=expires_at, nonce=nonce)


def csrf_matches(header_value: str | None, cookie_value: str | None) -> bool:
    """Double-submit check. Absent values never match."""
    if not header_value or not cookie_value:
        return False
    return hmac.compare_digest(header_value.encode("utf-8"), cookie_value.encode("utf-8"))


# ---------------------------------------------------------------------------
# Browser Host credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MintedCredential:
    """A freshly minted host credential.

    ``secret`` is returned to the operator exactly once and never persisted;
    only ``hashed`` and ``prefix`` are stored, so a database copy does not let
    an attacker drive the user's browser.
    """

    secret: str
    hashed: str
    prefix: str


def mint_host_credential() -> MintedCredential:
    """Generate a high-entropy Browser Host credential."""
    secret = secrets.token_urlsafe(HOST_CREDENTIAL_BYTES)
    return MintedCredential(
        secret=secret,
        hashed=hash_host_credential(secret),
        prefix=secret[:HOST_CREDENTIAL_PREFIX_LENGTH],
    )


def hash_host_credential(secret: str) -> str:
    """SHA-256 of a host credential.

    A plain digest rather than PBKDF2 on purpose: the credential is 256 bits of
    machine-generated entropy, so there is no dictionary to attack, and a host
    reconnect loop must not pay a 600k-iteration KDF on every attempt.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_host_credential(secret: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_host_credential(secret), hashed)


__all__ = [
    "HOST_CREDENTIAL_BYTES",
    "HOST_CREDENTIAL_PREFIX_LENGTH",
    "PBKDF2_ITERATIONS",
    "MintedCredential",
    "SessionFailure",
    "SessionToken",
    "WeakPasswordError",
    "csrf_matches",
    "hash_host_credential",
    "hash_password",
    "is_password_hash",
    "issue_session",
    "mint_host_credential",
    "session_material",
    "verify_configured_password",
    "verify_host_credential",
    "verify_password",
    "verify_session",
]
