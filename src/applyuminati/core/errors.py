"""Error taxonomy and failure classification.

Architectural rule 7: *a failure must remain inspectable rather than
disappearing into logs*. Every recoverable failure in Applyuminati is
represented as an :class:`ApplyuminatiError` carrying a
:class:`FailureCategory`, a machine-readable ``code``, a redaction-safe
``details`` mapping, and an explicit :class:`RecoveryHint`.

The category drives self-healing policy (retry / try another strategy /
escalate to the user), so it is part of the domain rather than an incidental
logging concern.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class FailureCategory(StrEnum):
    """Why something failed, in terms the recovery policy can act on."""

    #: The remote shape changed: selectors missing, schema drifted.
    EXTRACTION_DRIFT = "extraction_drift"
    #: Endpoint gone, 404/410, DNS failure, feed retired.
    ENDPOINT_UNAVAILABLE = "endpoint_unavailable"
    #: Transient network/5xx problem.
    TRANSIENT_NETWORK = "transient_network"
    #: Provider or site asked us to slow down.
    RATE_LIMITED = "rate_limited"
    #: Credentials absent, expired, or rejected.
    AUTH_REQUIRED = "auth_required"
    #: The site is actively refusing automated access.
    AUTOMATION_BLOCKED = "automation_blocked"
    #: A human challenge we will not attempt to defeat.
    HUMAN_CHALLENGE = "human_challenge"
    #: The selected backend (browser/agent/LLM/email) is not installed or not running.
    BACKEND_UNAVAILABLE = "backend_unavailable"
    #: A model returned output that failed schema validation.
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    #: The posting we were working on no longer exists.
    RESOURCE_GONE = "resource_gone"
    #: We would have repeated an action that already happened.
    DUPLICATE_ACTION = "duplicate_action"
    #: Local configuration is wrong or incomplete.
    CONFIGURATION = "configuration"
    #: Local persistence problem.
    STORAGE = "storage"
    #: The operation needs a decision only the user can make.
    NEEDS_HUMAN = "needs_human"
    #: Refused on policy grounds (e.g. would require fabricating a fact).
    POLICY_REFUSED = "policy_refused"
    #: Genuinely unclassified. Should trend towards zero.
    UNKNOWN = "unknown"


class RecoveryHint(StrEnum):
    """What the orchestrator should consider doing next."""

    RETRY = "retry"
    RETRY_AFTER_BACKOFF = "retry_after_backoff"
    TRY_ALTERNATIVE_STRATEGY = "try_alternative_strategy"
    DEGRADE = "degrade"
    ESCALATE_TO_USER = "escalate_to_user"
    ABORT = "abort"


#: Default policy mapping. Concrete strategies may override per attempt, but a
#: category always has a defensible default so nothing silently retries forever.
DEFAULT_RECOVERY: dict[FailureCategory, RecoveryHint] = {
    FailureCategory.EXTRACTION_DRIFT: RecoveryHint.TRY_ALTERNATIVE_STRATEGY,
    FailureCategory.ENDPOINT_UNAVAILABLE: RecoveryHint.TRY_ALTERNATIVE_STRATEGY,
    FailureCategory.TRANSIENT_NETWORK: RecoveryHint.RETRY_AFTER_BACKOFF,
    FailureCategory.RATE_LIMITED: RecoveryHint.RETRY_AFTER_BACKOFF,
    FailureCategory.AUTH_REQUIRED: RecoveryHint.ESCALATE_TO_USER,
    FailureCategory.AUTOMATION_BLOCKED: RecoveryHint.TRY_ALTERNATIVE_STRATEGY,
    FailureCategory.HUMAN_CHALLENGE: RecoveryHint.ESCALATE_TO_USER,
    FailureCategory.BACKEND_UNAVAILABLE: RecoveryHint.DEGRADE,
    FailureCategory.INVALID_MODEL_OUTPUT: RecoveryHint.RETRY,
    FailureCategory.RESOURCE_GONE: RecoveryHint.ABORT,
    FailureCategory.DUPLICATE_ACTION: RecoveryHint.ABORT,
    FailureCategory.CONFIGURATION: RecoveryHint.ESCALATE_TO_USER,
    FailureCategory.STORAGE: RecoveryHint.ABORT,
    FailureCategory.NEEDS_HUMAN: RecoveryHint.ESCALATE_TO_USER,
    FailureCategory.POLICY_REFUSED: RecoveryHint.ABORT,
    FailureCategory.UNKNOWN: RecoveryHint.ESCALATE_TO_USER,
}

#: Categories where repeating the identical request can plausibly succeed.
RETRYABLE = frozenset(
    {
        FailureCategory.TRANSIENT_NETWORK,
        FailureCategory.RATE_LIMITED,
        FailureCategory.INVALID_MODEL_OUTPUT,
    }
)


class ApplyuminatiError(Exception):
    """Base class for every failure Applyuminati raises deliberately."""

    category: FailureCategory = FailureCategory.UNKNOWN

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        category: FailureCategory | None = None,
        details: dict[str, Any] | None = None,
        recovery: RecoveryHint | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.category = category or type(self).category
        self.code = code or f"{self.category.value}.generic"
        self.details = details or {}
        self.recovery = recovery or DEFAULT_RECOVERY[self.category]
        self.retry_after_seconds = retry_after_seconds

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE

    def to_dict(self) -> dict[str, Any]:
        """Redaction-safe structured form, suitable for the failure log and API."""
        from applyuminati.core.redaction import redact_mapping

        return {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "recovery": self.recovery.value,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "details": redact_mapping(self.details),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ConfigurationError(ApplyuminatiError):
    category = FailureCategory.CONFIGURATION


class StorageError(ApplyuminatiError):
    category = FailureCategory.STORAGE


class BackendUnavailableError(ApplyuminatiError):
    category = FailureCategory.BACKEND_UNAVAILABLE


class SourceError(ApplyuminatiError):
    """Raised by job-source plugins. Subclass or pass an explicit category."""

    category = FailureCategory.UNKNOWN


class ExtractionDriftError(SourceError):
    category = FailureCategory.EXTRACTION_DRIFT


class RateLimitedError(ApplyuminatiError):
    category = FailureCategory.RATE_LIMITED


class TransientNetworkError(ApplyuminatiError):
    category = FailureCategory.TRANSIENT_NETWORK


class EndpointUnavailableError(ApplyuminatiError):
    category = FailureCategory.ENDPOINT_UNAVAILABLE


class AuthenticationRequiredError(ApplyuminatiError):
    category = FailureCategory.AUTH_REQUIRED


class AutomationBlockedError(ApplyuminatiError):
    """The site is refusing automated access.

    Applyuminati never attempts to defeat an access control. This error exists
    so the condition is *recorded and surfaced*, and so the orchestrator can
    fall back to a supported backend or ask the user to take over.
    """

    category = FailureCategory.AUTOMATION_BLOCKED


class HumanChallengeError(ApplyuminatiError):
    """A CAPTCHA or similar challenge was detected. We stop and tell the user."""

    category = FailureCategory.HUMAN_CHALLENGE


class InvalidModelOutputError(ApplyuminatiError):
    category = FailureCategory.INVALID_MODEL_OUTPUT


class DuplicateActionError(ApplyuminatiError):
    category = FailureCategory.DUPLICATE_ACTION


class NeedsHumanError(ApplyuminatiError):
    category = FailureCategory.NEEDS_HUMAN


class FabricationRefusedError(ApplyuminatiError):
    """Generation was rejected because it asserted a fact with no evidence."""

    category = FailureCategory.POLICY_REFUSED


class NotFoundError(ApplyuminatiError):
    category = FailureCategory.RESOURCE_GONE


__all__ = [
    "DEFAULT_RECOVERY",
    "RETRYABLE",
    "ApplyuminatiError",
    "AuthenticationRequiredError",
    "AutomationBlockedError",
    "BackendUnavailableError",
    "ConfigurationError",
    "DuplicateActionError",
    "EndpointUnavailableError",
    "ExtractionDriftError",
    "FabricationRefusedError",
    "FailureCategory",
    "HumanChallengeError",
    "InvalidModelOutputError",
    "NeedsHumanError",
    "NotFoundError",
    "RateLimitedError",
    "RecoveryHint",
    "SourceError",
    "StorageError",
    "TransientNetworkError",
]
