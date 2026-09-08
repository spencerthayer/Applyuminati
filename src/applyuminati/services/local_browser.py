"""Process-owned browser execution for attempts PR #9 selected locally.

A local session is the process's own: it dies with the process, and honest
restart handling says so. Host-backed sessions stay host-owned and never touch
this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from applyuminati.browser.base import (
    BROWSER_REGISTRY,
    BrowserBackend,
    BrowserCapability,
    BrowserSession,
)
from applyuminati.core.errors import BackendUnavailableError
from applyuminati.core.logging import get_logger
from applyuminati.core.models.execution import ApplicationAttempt
from applyuminati.core.settings import Settings

log = get_logger(__name__)

__all__ = ["LocalBrowserManager", "LocalSession"]


@dataclass(slots=True)
class LocalSession:
    """One live process-owned session and the backend that owns it."""

    backend_slug: str
    session: BrowserSession


class LocalBrowserManager:
    """Own the process's local browser backends and the sessions it opened.

    One backend per selected slug for the life of the process. Acquisition
    re-checks the persisted requirements snapshot against the selected
    backend's declared capabilities before opening anything: the selection was
    PR #9's decision, and acquisition never substitutes another backend.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._backends: dict[str, BrowserBackend] = {}
        self._sessions: dict[str, LocalSession] = {}

    async def acquire(self, attempt: ApplicationAttempt) -> BrowserSession:
        """Open (or re-enter) the process-owned session for this attempt."""
        slug = attempt.browser_backend
        if not slug:
            raise BackendUnavailableError(
                "attempt has no persisted browser selection to execute",
                code="browser.selection_missing",
            )
        backend = self._backend_for(slug, attempt)
        existing = self._sessions.get(attempt.id)
        if existing is not None and existing.backend_slug == slug:
            # A task retry within this process re-enters the same session.
            return existing.session
        session = await backend.open_session(session_id=attempt.id)
        self._sessions[attempt.id] = LocalSession(backend_slug=slug, session=session)
        if existing is not None:
            # The persisted backend changed, which durable selection makes
            # impossible. Surface it rather than open silently over it.
            log.warning(
                "attempt.local_session_backend_changed",
                attempt_id=attempt.id,
                previous=existing.backend_slug,
                backend=slug,
            )
        else:
            log.info(
                "attempt.local_session_opened",
                attempt_id=attempt.id,
                backend=slug,
            )
        return session

    def owns(self, attempt_id: str) -> bool:
        return attempt_id in self._sessions

    def release(self, attempt_id: str) -> None:
        """Drop the registry entry. The session itself closes with the backend."""
        self._sessions.pop(attempt_id, None)

    def _backend_for(self, slug: str, attempt: ApplicationAttempt) -> BrowserBackend:
        backend = self._backends.get(slug)
        if backend is not None:
            self._verify_contract(backend, attempt)
            return backend
        descriptor = BROWSER_REGISTRY.try_get(slug)
        if descriptor is None:
            raise BackendUnavailableError(
                f"selected browser backend {slug!r} is not registered",
                code="browser.selection_unregistered",
                details={"backend": slug},
            )
        backend = descriptor.create(settings=self._settings)
        self._verify_contract(backend, attempt)
        self._backends[slug] = backend
        return backend

    def _verify_contract(self, backend: BrowserBackend, attempt: ApplicationAttempt) -> None:
        """Re-check the persisted snapshot. Never substitute, never downgrade."""
        snapshot = attempt.browser_requirements
        if not snapshot:
            return
        missing = [
            name
            for name in snapshot.get("required", [])
            if not backend.metadata.supports(BrowserCapability(name))
        ]
        if missing:
            raise BackendUnavailableError(
                f"selected backend {backend.metadata.slug!r} does not satisfy "
                f"the persisted contract; missing: {', '.join(missing)}",
                code="browser.selection_contract_unmet",
                details={"backend": backend.metadata.slug, "missing": missing},
            )

    async def aclose(self) -> None:
        """Close every local backend. Idempotent."""
        self._sessions.clear()
        for slug, backend in self._backends.items():
            try:
                await backend.aclose()
            except Exception:  # shutdown must not raise
                log.warning("local_browser.backend_close_failed", backend=slug)
        self._backends.clear()
