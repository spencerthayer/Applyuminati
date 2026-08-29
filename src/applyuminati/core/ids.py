"""Stable, sortable identifiers.

Applyuminati uses ULIDs (Crockford base32, 128 bit, lexicographically
sortable by creation time) for every entity, run and task. Sortable IDs make
event logs and task tables readable without an extra ordering column, and they
are safe to generate offline in multiple processes.

Implemented locally rather than pulled from a dependency: it is 40 lines and
the dependency surface of this project is already large.
"""

from __future__ import annotations

import hashlib
import os
import re

from applyuminati.core.clock import utcnow

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32, no I L O U
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _encode(value: int, length: int) -> str:
    chars = [""] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def new_ulid() -> str:
    """Return a fresh 26-character ULID."""
    timestamp_ms = int(utcnow().timestamp() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(timestamp_ms, 10) + _encode(randomness, 16)


def is_ulid(value: str) -> bool:
    """Return ``True`` if ``value`` looks like a ULID produced by :func:`new_ulid`."""
    return bool(_ULID_RE.match(value))


def stable_id(namespace: str, *parts: str) -> str:
    """Return a deterministic 26-character identifier derived from ``parts``.

    Used where an identity must be reproducible across runs — for example the
    natural key of a job posting, so re-running discovery updates a row rather
    than inserting a duplicate. The namespace prevents collisions between
    different kinds of derived key.
    """
    digest = hashlib.blake2b(
        "\x1f".join((namespace, *parts)).encode("utf-8"), digest_size=16
    ).digest()
    return _encode(int.from_bytes(digest, "big"), 26)


__all__ = ["is_ulid", "new_ulid", "stable_id"]
