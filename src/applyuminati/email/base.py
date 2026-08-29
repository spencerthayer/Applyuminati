"""Email provider contract.

Reading a mailbox and reasoning about what an employer said are separate
concerns. This module owns the first: a provider yields
:class:`EmailMessage` objects and nothing else. Classification, status
inference and reply drafting live in the application layer and work
identically over Gmail, Microsoft 365 or plain IMAP.

Nothing here sends mail. Drafting is supported; delivery stays under explicit
user control, so :meth:`EmailProvider.save_draft` exists and ``send`` does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.provenance import Confidence
from applyuminati.core.registry import HealthReport, PluginDescriptor, Registry


class EmailCapability(StrEnum):
    LIST_MESSAGES = "list_messages"
    SEARCH = "search"
    FETCH_BODY = "fetch_body"
    LIST_ATTACHMENTS = "list_attachments"
    LABELS = "labels"
    #: Can write a draft into the user's mailbox for them to review and send.
    SAVE_DRAFT = "save_draft"
    #: Supports server-side push/notification rather than polling.
    PUSH = "push"


class EmailAddress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    address: str
    display_name: str | None = None

    @property
    def domain(self) -> str:
        _, _, domain = self.address.partition("@")
        return domain.lower()


class EmailMessage(BaseModel):
    """One message, normalised across providers."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: Provider's own identifier, used for idempotent re-ingestion.
    provider_message_id: str
    account: str
    #: RFC 5322 Message-ID, for threading across providers.
    rfc_message_id: str | None = None
    thread_id: str | None = None
    sender: EmailAddress
    recipients: list[EmailAddress] = Field(default_factory=list)
    subject: str = ""
    received_at: datetime = Field(default_factory=utcnow)
    #: Plain-text body. HTML is converted at ingestion; never stored raw.
    body_text: str | None = None
    snippet: str | None = None
    labels: list[str] = Field(default_factory=list)
    has_attachments: bool = False
    #: URLs extracted from the body, for assessment/interview links.
    links: list[str] = Field(default_factory=list)


class EmailQuery(BaseModel):
    """A provider-agnostic mailbox query."""

    model_config = ConfigDict(extra="forbid")

    since: datetime | None = None
    until: datetime | None = None
    #: Free-text terms; providers map these onto their own search syntax.
    terms: list[str] = Field(default_factory=list)
    from_domains: list[str] = Field(default_factory=list)
    mailbox: str | None = None
    max_results: int = 100
    #: Opaque cursor for incremental sync.
    cursor: str | None = None


class EmailFetchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str
    messages: list[EmailMessage] = Field(default_factory=list)
    next_cursor: str | None = None
    truncated: bool = False
    #: Failures captured rather than raised, matching the job-source contract.
    failures: list[str] = Field(default_factory=list)


class EmailClass(StrEnum):
    """What an employer message appears to be.

    Classification is a separate application-layer concern; the enum lives
    here because both the classifier and the status-inference table need it.
    """

    APPLICATION_CONFIRMATION = "application_confirmation"
    RECRUITER_OUTREACH = "recruiter_outreach"
    REJECTION = "rejection"
    ASSESSMENT_REQUEST = "assessment_request"
    INTERVIEW_REQUEST = "interview_request"
    SCHEDULING = "scheduling"
    OFFER = "offer"
    INFORMATION_REQUEST = "information_request"
    MARKETING = "marketing"
    UNRELATED = "unrelated"


class MessageClassification(BaseModel):
    """A classified message, linked to an application when we can tell."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    email_class: EmailClass
    confidence: Confidence = 0.0
    application_id: str | None = None
    job_id: str | None = None
    company_key: str | None = None
    #: Dates and links extracted from the body.
    extracted_dates: list[datetime] = Field(default_factory=list)
    extracted_links: list[str] = Field(default_factory=list)
    #: Proposed application state change. Applied only after policy review.
    suggested_state: str | None = None
    rationale: str = ""


class EmailDraft(BaseModel):
    """A reply prepared for the user. Never sent automatically."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    in_reply_to_message_id: str | None = None
    to: list[EmailAddress] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    application_id: str | None = None
    #: Claim ids grounding any factual statement in the body.
    evidence_claim_ids: list[str] = Field(default_factory=list)
    prompt_version: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class EmailProviderMetadata:
    slug: str
    name: str
    capabilities: frozenset[EmailCapability]
    requires_oauth: bool = False
    homepage: str | None = None
    notes: str = ""
    #: Options schema keys the account config must supply.
    required_config: tuple[str, ...] = field(default_factory=tuple)

    def supports(self, capability: EmailCapability) -> bool:
        return capability in self.capabilities


@runtime_checkable
class EmailProvider(Protocol):
    """The interface every mailbox adapter implements."""

    @property
    def metadata(self) -> EmailProviderMetadata: ...

    async def health(self) -> HealthReport:
        """Connectivity and credential probe."""
        ...

    async def fetch(self, query: EmailQuery) -> EmailFetchResult:
        """Retrieve messages. Failures are captured, not raised."""
        ...

    async def save_draft(self, draft: EmailDraft) -> str:
        """Write a draft into the user's mailbox; returns the provider id."""
        ...

    async def aclose(self) -> None: ...


#: The process-wide email provider registry.
EMAIL_REGISTRY: Registry[EmailProvider] = Registry("email", entry_point_group="applyuminati.email")


def email_plugin(
    *,
    slug: str,
    name: str,
    factory: Any,  # noqa: ANN401
    capabilities: frozenset[EmailCapability],
    description: str = "",
    requires_auth: bool = True,
    priority: int = 0,
) -> PluginDescriptor[EmailProvider]:
    return PluginDescriptor[EmailProvider](
        slug=slug,
        name=name,
        kind="email",
        factory=factory,
        description=description,
        capabilities=frozenset(c.value for c in capabilities),
        requires_auth=requires_auth,
        priority=priority,
    )


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
    "email_plugin",
]
