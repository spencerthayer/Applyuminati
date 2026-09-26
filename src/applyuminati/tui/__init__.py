"""Terminal UI for Applyuminati.

Talks to the services in process, the way the CLI does, so it needs no
running server, no port, and no password.
"""

from __future__ import annotations


def __getattr__(name: str) -> object:
    # Lazy so a headless install without the `tui` extra still gets a working
    # CLI, and the failure is a clear message rather than an import error at
    # startup.
    if name == "TuiApp":
        from applyuminati.tui.app import TuiApp

        return TuiApp
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
