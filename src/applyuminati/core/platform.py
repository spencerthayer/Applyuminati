"""The host platform, as one overridable function.

``sys.platform`` scattered through the codebase is untestable: the ego lite
backend is macOS-only, browser selection has to reject it on Linux, and a
Browser Host reports its platform on registration. All three need to be
exercised from CI, which runs on Linux, so all three read the platform through
here and tests monkeypatch one place.

The values are ``sys.platform`` values (``darwin``, ``linux``, ``win32``) rather
than a new vocabulary, because that is what a Node process, a Docker label and
every other component involved already reports.
"""

from __future__ import annotations

import os
import sys

__all__ = ["PLATFORM_OVERRIDE_ENV", "current_platform", "is_macos"]

#: Escape hatch for reproducing a platform-specific decision on another host.
#: Only affects capability and selection logic; it cannot make a macOS-only
#: helper appear, so a lie here surfaces as a health-probe failure rather than
#: as a wrong answer.
PLATFORM_OVERRIDE_ENV = "APPLYUMINATI_PLATFORM"


def current_platform() -> str:
    """This host's ``sys.platform`` value, honouring the override."""
    return os.environ.get(PLATFORM_OVERRIDE_ENV) or sys.platform


def is_macos() -> bool:
    return current_platform() == "darwin"
