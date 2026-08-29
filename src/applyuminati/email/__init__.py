"""Email provider contract, classification, and selection."""

from applyuminati.email.base import (
    EMAIL_REGISTRY,
    EmailAddress,
    EmailCapability,
    EmailClass,
    EmailDraft,
    EmailFetchResult,
    EmailMessage,
    EmailProvider,
    EmailProviderMetadata,
    EmailQuery,
    MessageClassification,
    email_plugin,
)
from applyuminati.email.classify import classify_message

__all__ = [
    "EMAIL_REGISTRY",
    "EmailAddress",
    "EmailCapability",
    "EmailClass",
    "EmailDraft",
    "EmailFetchResult",
    "EmailMessage",
    "EmailProvider",
    "EmailProviderMetadata",
    "EmailQuery",
    "MessageClassification",
    "classify_message",
    "email_plugin",
]
