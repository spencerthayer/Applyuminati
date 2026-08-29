"""Job-source contract.

A source plugin is one object satisfying :class:`JobSource`. It declares what
it can do through :class:`SourceMetadata` and returns a :class:`SourceResult`.

The defining rule, learned from every reference implementation that got this
wrong: **discovery never raises for an expected failure**. A rate limit, a
layout change or a dead endpoint is data — it lands in
:attr:`SourceResult.failures` so the run continues, the condition is visible
in the UI, and workflow memory gets a record. Only genuine programming errors
propagate.

Adding a source must not require touching scoring, resume, application or UI
code. The only shared surface is this module plus
:mod:`applyuminati.sources.normalize`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.errors import ApplyuminatiError, FailureCategory
from applyuminati.core.models.common import EmploymentType, RemoteMode
from applyuminati.core.models.job import AtsVendor, Job, SourceTier, VerificationState
from applyuminati.core.registry import HealthReport, PluginDescriptor, Registry
from applyuminati.core.strategy import SearchStrategy


class SourceCapability(StrEnum):
    """What a source can do. Consumers branch on these, never on the slug."""

    #: Can list postings for a configured employer/board.
    LIST_BY_EMPLOYER = "list_by_employer"
    #: Accepts free-text keyword queries.
    KEYWORD_SEARCH = "keyword_search"
    #: Accepts a location filter server-side.
    LOCATION_FILTER = "location_filter"
    #: Can return the full description without a second fetch.
    FULL_DESCRIPTION = "full_description"
    #: Supports paging beyond the first result set.
    PAGINATION = "pagination"
    #: Can cheaply re-check whether a single posting is still live.
    FRESHNESS_CHECK = "freshness_check"
    #: Reports a structured compensation range.
    COMPENSATION = "compensation"
    #: Needs a browser backend rather than plain HTTP.
    REQUIRES_BROWSER = "requires_browser"
    #: Needs user credentials.
    REQUIRES_AUTH = "requires_auth"
    #: Reads from local files rather than the network.
    OFFLINE = "offline"


class BlockingBehavior(StrEnum):
    """Known automation-blocking posture of a source.

    Recorded so the orchestrator can choose a supported strategy up front.
    Applyuminati never attempts to defeat any of these.
    """

    NONE = "none"
    #: Rate limits, but otherwise permits automated access.
    RATE_LIMITED = "rate_limited"
    #: Requires a signed-in session.
    LOGIN_WALL = "login_wall"
    #: Serves interstitials or bot checks to non-browser clients.
    BOT_CHALLENGE = "bot_challenge"
    #: Terms of service prohibit automated collection; plugin is detect-only.
    PROHIBITED = "prohibited"


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Politeness budget a plugin promises to stay within."""

    requests_per_minute: float = 30.0
    concurrent_requests: int = 2
    #: Seconds to wait after a 429 when the server sends no ``Retry-After``.
    backoff_seconds: float = 30.0

    @property
    def min_interval_seconds(self) -> float:
        return 60.0 / self.requests_per_minute if self.requests_per_minute > 0 else 0.0


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Everything the rest of the system needs to know about a source."""

    slug: str
    name: str
    tier: SourceTier
    description: str = ""
    capabilities: frozenset[SourceCapability] = field(default_factory=frozenset)
    #: ATS behind the postings, when the source is ATS-specific.
    ats: AtsVendor = AtsVendor.UNKNOWN
    rate_limit: RateLimit = field(default_factory=RateLimit)
    blocking: BlockingBehavior = BlockingBehavior.NONE
    requires_auth: bool = False
    #: Documentation or terms URL, shown in the settings UI.
    homepage: str | None = None
    #: Hints the application operator can use, e.g. ``{"form": "greenhouse_v1"}``.
    application_hints: dict[str, str] = field(default_factory=dict)
    #: Fallback source slugs to try when this one is blocked or gone.
    fallbacks: tuple[str, ...] = ()

    def supports(self, capability: SourceCapability) -> bool:
        return capability in self.capabilities


class DiscoveryRequest(BaseModel):
    """What to look for. Derived from the profile and the active strategy."""

    model_config = ConfigDict(extra="forbid")

    #: Free-text titles/keywords, most important first.
    queries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_modes: list[RemoteMode] = Field(default_factory=list)
    employment_types: list[EmploymentType] = Field(default_factory=list)
    #: Ignore postings older than this. ``None`` means no age filter.
    posted_within_days: int | None = None
    #: Hard ceiling per source, so one chatty board cannot dominate a run.
    max_results: int = 200
    #: Pagination depth, derived from ``strategy.depth_bias``.
    max_pages: int = 3
    #: Opaque cursor from a previous partial run, enabling resumption.
    cursor: str | None = None
    #: Plugin-specific options from settings, already validated.
    options: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None

    @classmethod
    def from_strategy(
        cls,
        strategy: SearchStrategy,
        *,
        queries: list[str],
        locations: list[str] | None = None,
        options: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> DiscoveryRequest:
        return cls(
            queries=queries,
            locations=locations or [],
            max_results=strategy.max_jobs_per_source_per_run,
            max_pages=strategy.pages_per_source,
            options=options or {},
            run_id=run_id,
        )


class SourceFailure(BaseModel):
    """A failure that happened inside a source, captured rather than raised."""

    model_config = ConfigDict(extra="forbid")

    source: str
    category: FailureCategory
    message: str
    #: Stage within the plugin: ``list``, ``paginate``, ``detail``, ``parse``.
    stage: str = "list"
    #: Strategy that failed, so an alternative can be selected next time.
    strategy: str | None = None
    url: str | None = None
    retryable: bool = False
    retry_after_seconds: float | None = None
    occurred_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_error(
        cls, source: str, error: ApplyuminatiError, *, stage: str = "list", url: str | None = None
    ) -> SourceFailure:
        return cls(
            source=source,
            category=error.category,
            message=error.message,
            stage=stage,
            url=url,
            retryable=error.retryable,
            retry_after_seconds=error.retry_after_seconds,
            details=error.details,
        )


class SourceResult(BaseModel):
    """Everything one discovery call produced, successes and failures alike."""

    model_config = ConfigDict(extra="forbid")

    source: str
    jobs: list[Job] = Field(default_factory=list)
    failures: list[SourceFailure] = Field(default_factory=list)
    #: Set when results were truncated, so "0 new jobs" is never ambiguous.
    truncated: bool = False
    #: Cursor to resume from; persisted in the task's ``resume_state``.
    next_cursor: str | None = None
    pages_fetched: int = 0
    requests_made: int = 0
    duration_seconds: float | None = None

    @property
    def ok(self) -> bool:
        """True when nothing failed. Partial success is not success."""
        return not self.failures

    @property
    def partial(self) -> bool:
        return bool(self.jobs) and bool(self.failures)

    def summary(self) -> str:
        parts = [f"{len(self.jobs)} jobs"]
        if self.failures:
            parts.append(f"{len(self.failures)} failures")
        if self.truncated:
            parts.append("truncated")
        return ", ".join(parts)


class FreshnessResult(BaseModel):
    """Outcome of re-checking a single posting."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    state: VerificationState
    checked_at: datetime = Field(default_factory=utcnow)
    detail: str | None = None


@runtime_checkable
class JobSource(Protocol):
    """The interface every job-source plugin implements."""

    @property
    def metadata(self) -> SourceMetadata:
        """Static description of this source."""
        ...

    async def health(self) -> HealthReport:
        """Cheap availability probe. Must not perform a full discovery."""
        ...

    async def discover(self, request: DiscoveryRequest) -> SourceResult:
        """Find postings.

        Implementations must not raise for expected failures; they populate
        :attr:`SourceResult.failures` instead.
        """
        ...

    async def verify(self, job: Job) -> FreshnessResult:
        """Re-check whether one posting is still live.

        Sources without :attr:`SourceCapability.FRESHNESS_CHECK` return
        :attr:`VerificationState.UNKNOWN` rather than guessing.
        """
        ...


#: The process-wide job-source registry.
SOURCE_REGISTRY: Registry[JobSource] = Registry("source", entry_point_group="applyuminati.sources")


def source_plugin(
    *,
    slug: str,
    name: str,
    factory: Any,
    description: str = "",
    capabilities: frozenset[SourceCapability] = frozenset(),
    options_schema: type[BaseModel] | None = None,
    requires_auth: bool = False,
    priority: int = 0,
) -> PluginDescriptor[JobSource]:
    """Build a descriptor for a job-source plugin."""
    return PluginDescriptor[JobSource](
        slug=slug,
        name=name,
        kind="source",
        factory=factory,
        description=description,
        capabilities=frozenset(c.value for c in capabilities),
        options_schema=options_schema,
        requires_auth=requires_auth,
        priority=priority,
    )


__all__ = [
    "SOURCE_REGISTRY",
    "BlockingBehavior",
    "DiscoveryRequest",
    "FreshnessResult",
    "JobSource",
    "RateLimit",
    "SourceCapability",
    "SourceFailure",
    "SourceMetadata",
    "SourceResult",
    "source_plugin",
]
