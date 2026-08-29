"""Redaction of secrets and sensitive career data.

Applied at the logging boundary and by :meth:`ApplyuminatiError.to_dict`, so a
stack trace or a structured log line never carries an API key, a session
cookie, a full resume, or a sensitive questionnaire answer.

This is defence in depth, not a licence to pass secrets around: code should
still avoid putting them in ``details`` in the first place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "***redacted***"

#: Substrings that mark a mapping key as secret-bearing.
SECRET_KEY_HINTS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth_token",
        "access_token",
        "refresh_token",
        "bearer",
        "client_secret",
        "cookie",
        "credential",
        "id_token",
        "passphrase",
        "password",
        "private_key",
        "secret",
        "session",
        "ssn",
        "token",
    }
)

#: Keys whose *content* is personal or bulky enough that we never log it whole.
SENSITIVE_KEY_HINTS: frozenset[str] = frozenset(
    {
        "answer",
        "cover_letter",
        "date_of_birth",
        "demographic",
        "disability",
        "eeo",
        "national_id",
        "resume",
        "resume_json",
        "salary_expectation",
        "veteran",
    }
)

_BEARER_RE = re.compile(r"(?i)\b(bearer|token|api[-_ ]?key)\b\s*[:=]?\s*\S+")
_OPENAI_STYLE_RE = re.compile(r"\b(sk|rk|xoxb|ghp|gho|github_pat)[-_][A-Za-z0-9_-]{12,}\b")
_MAX_STRING = 512


def _key_matches(key: str, hints: frozenset[str]) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in hints)


def redact_text(value: str) -> str:
    """Scrub token-shaped substrings and truncate very long strings."""
    scrubbed = _OPENAI_STYLE_RE.sub(REDACTED, value)
    scrubbed = _BEARER_RE.sub(REDACTED, scrubbed)
    if len(scrubbed) > _MAX_STRING:
        return f"{scrubbed[:_MAX_STRING]}…[truncated {len(scrubbed) - _MAX_STRING} chars]"
    return scrubbed


def redact_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact an arbitrary structure."""
    if depth > 6:
        return "…[depth limit]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return redact_mapping(value, depth=depth + 1)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, Sequence):
        items = [redact_value(item, depth=depth + 1) for item in value[:50]]
        if len(value) > 50:
            items.append(f"…[{len(value) - 50} more]")
        return items
    return value


def redact_mapping(mapping: Mapping[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Return a copy of ``mapping`` with secret and sensitive values removed."""
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if _key_matches(key, SECRET_KEY_HINTS):
            result[key] = REDACTED
        elif _key_matches(key, SENSITIVE_KEY_HINTS):
            result[key] = f"<{type(value).__name__} withheld>"
        else:
            result[key] = redact_value(value, depth=depth)
    return result


__all__ = [
    "REDACTED",
    "SECRET_KEY_HINTS",
    "SENSITIVE_KEY_HINTS",
    "redact_mapping",
    "redact_text",
    "redact_value",
]
