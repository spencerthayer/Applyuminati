"""Turning a site-chosen filename into a path we are willing to write.

The suggested filename on a download is the only string in the whole browser
contract that a remote employer portal controls end to end, and it arrives
already looking like a path. ``../../.ssh/authorized_keys`` is a valid
``Content-Disposition`` filename; so is ``.bashrc``, so is a name long enough to
blow past ``NAME_MAX``, so is one containing a NUL byte.

So the suggestion is treated as a label, never as a location. This module
derives a name from it and joins that name to a directory the caller already
trusts, and :func:`resolve_download_path` re-checks the result against that
directory afterwards rather than assuming the derivation was airtight.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

__all__ = [
    "DEFAULT_DOWNLOAD_NAME",
    "MAX_FILENAME_CHARS",
    "ensure_download_directory",
    "resolve_download_path",
    "safe_download_filename",
]

#: Used when the suggestion holds nothing usable. Not derived from the URL,
#: which is equally remote-controlled.
DEFAULT_DOWNLOAD_NAME = "download"

#: Well under the 255-byte ``NAME_MAX`` on every filesystem we target, with room
#: for the ``-2``, ``-3`` suffixes collision handling appends.
MAX_FILENAME_CHARS = 120

#: Anything outside this is replaced. Allowing only known-safe characters, so a
#: separator or control byte nobody thought of is not a new bypass.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: What is left of a traversal attempt after the separators are gone. Chromium
#: scrubs ``../../x`` to ``.._.._x`` before we ever see it, and keeping that
#: verbatim would store a file whose name reads like an exploit for no reason.
#: Inert either way; this is about the name being legible.
_DOT_RUNS = re.compile(r"\.{2,}")


def safe_download_filename(suggested: str | None) -> str:
    """A single path component derived from a site-supplied filename.

    Never empty, never ``.`` or ``..``, never containing a separator, and never
    beginning with a dot. Both POSIX and Windows separators are stripped: a
    Linux container can be handed ``..\\..\\evil`` by a site that assumed a
    Windows client, and ``PurePosixPath`` alone treats that as one long name.
    """
    raw = (suggested or "").strip()
    # Take the last component under both separator conventions before scrubbing,
    # so a directory prefix is discarded rather than flattened into the name.
    name = PureWindowsPath(PurePosixPath(raw).name).name
    name = _UNSAFE.sub("_", name)
    name = _DOT_RUNS.sub(".", name)
    # Both characters, in one pass. Stripping dots and then underscores would
    # uncover a fresh leading dot in `._.bashrc`, which is exactly the name this
    # rule exists to refuse.
    name = name.strip("._")
    if not name or name in {".", ".."}:
        return DEFAULT_DOWNLOAD_NAME
    if len(name) <= MAX_FILENAME_CHARS:
        return name
    # Truncate the stem rather than the whole name: the extension is what tells
    # a later reader (and the user) what the file is.
    stem, dot, suffix = name.rpartition(".")
    if not dot or len(suffix) > 16:
        return name[:MAX_FILENAME_CHARS]
    keep = MAX_FILENAME_CHARS - len(suffix) - 1
    return f"{stem[:keep]}.{suffix}" if keep > 0 else name[:MAX_FILENAME_CHARS]


def ensure_download_directory(root: Path, directory: Path) -> Path:
    """Create ``directory`` only after proving it sits under ``root``.

    ``Path.mkdir`` on ``root / "../../outside"`` would create the outside
    directory before any later check could refuse it, so the check comes first.
    ``directory.resolve()`` computes the would-be location without creating it.
    A caller-supplied session id is not a trusted path component; this is the
    gate that keeps it from becoming one.
    """
    resolved_root = root.resolve()
    resolved_dir = directory.resolve()
    if resolved_dir != resolved_root and not resolved_dir.is_relative_to(resolved_root):
        msg = f"download directory {directory} is outside {root}"
        raise ValueError(msg)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    return resolved_dir


def resolve_download_path(root: Path, directory: Path, suggested: str | None) -> Path:
    """Where to write a download, guaranteed to sit under ``root``.

    ``directory`` is the caller's chosen subdirectory (a session id, say) and is
    checked too, because a caller that built it from remote data would otherwise
    escape by the back door. Existing files are never overwritten: two
    applications both downloading ``offer.pdf`` are two files, and silently
    replacing the first is how evidence of an earlier attempt disappears.
    """
    resolved_root = root.resolve()
    resolved_dir = directory.resolve()
    if resolved_dir != resolved_root and not resolved_dir.is_relative_to(resolved_root):
        msg = f"download directory {directory} is outside {root}"
        raise ValueError(msg)

    name = safe_download_filename(suggested)
    candidate = resolved_dir / name
    stem, dot, suffix = name.rpartition(".")
    counter = 2
    while candidate.exists():
        candidate = resolved_dir / (f"{stem}-{counter}.{suffix}" if dot else f"{name}-{counter}")
        counter += 1

    # Redundant if `safe_download_filename` did its job, which is the point:
    # this is the assertion that the derivation held, checked against the root
    # rather than trusted.
    if not candidate.is_relative_to(resolved_root):
        msg = f"refusing to write {suggested!r} outside {root}"
        raise ValueError(msg)
    return candidate
