"""First-party email provider adapters."""

from __future__ import annotations

from applyuminati.email.base import EMAIL_REGISTRY


def register_email_providers() -> None:
    """Register built-in email providers. Idempotent."""
    if "imap" not in EMAIL_REGISTRY:
        from applyuminati.plugins.email.imap import PLUGIN as imap

        EMAIL_REGISTRY.register(imap)


__all__ = ["register_email_providers"]
