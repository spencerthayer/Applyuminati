"""Company and role research.

Research is external, third-party information. It ages, it is often wrong,
and it must never be presented as though the user verified it. Every finding
therefore carries provenance and a TTL, and
:meth:`CompanyResearch.current_findings` filters out anything stale rather
than quietly serving a two-year-old headcount as current.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.provenance import AssertionLevel, Confidence, Provenance

#: Default freshness horizons in seconds, per topic. Sentiment and funding
#: news go stale fast; a company's founding year does not.
DEFAULT_TTL_SECONDS: dict[str, int] = {
    "description": 180 * 86400,
    "products": 90 * 86400,
    "industry": 365 * 86400,
    "size": 90 * 86400,
    "locations": 180 * 86400,
    "funding": 30 * 86400,
    "public_status": 90 * 86400,
    "recent_developments": 14 * 86400,
    "compensation": 60 * 86400,
    "employee_sentiment": 60 * 86400,
    "interview_reports": 120 * 86400,
    "layoffs": 21 * 86400,
    "role_context": 45 * 86400,
}


class ResearchTopic(StrEnum):
    DESCRIPTION = "description"
    PRODUCTS = "products"
    INDUSTRY = "industry"
    SIZE = "size"
    LOCATIONS = "locations"
    FUNDING = "funding"
    PUBLIC_STATUS = "public_status"
    RECENT_DEVELOPMENTS = "recent_developments"
    COMPENSATION = "compensation"
    EMPLOYEE_SENTIMENT = "employee_sentiment"
    INTERVIEW_REPORTS = "interview_reports"
    LAYOFFS = "layoffs"
    ROLE_CONTEXT = "role_context"


class ResearchFinding(BaseModel):
    """One statement about a company or role, with its shelf life."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    topic: ResearchTopic
    statement: str
    #: External research by construction; upgraded only by explicit user action.
    level: AssertionLevel = AssertionLevel.EXTERNAL_RESEARCH
    provenance: list[Provenance] = Field(default_factory=list)
    confidence: Confidence = 0.5
    collected_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None

    def is_stale(self, *, now: datetime | None = None) -> bool:
        reference = now or utcnow()
        if self.expires_at is not None:
            return reference >= self.expires_at
        ttl = DEFAULT_TTL_SECONDS.get(self.topic.value)
        if ttl is None:
            return False
        return (reference - self.collected_at).total_seconds() > ttl

    def age_days(self, *, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.collected_at).total_seconds() / 86400.0


class CompanyResearch(BaseModel):
    """Cached research about one employer."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: Normalised company key, matching :attr:`Job.company_key`.
    company_key: str
    display_name: str
    domain: str | None = None
    findings: list[ResearchFinding] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def current_findings(self, *, now: datetime | None = None) -> list[ResearchFinding]:
        """Findings that are still within their freshness horizon."""
        return [f for f in self.findings if not f.is_stale(now=now)]

    def stale_topics(self, *, now: datetime | None = None) -> list[ResearchTopic]:
        """Topics we hold only stale data for — candidates for a refresh."""
        fresh = {f.topic for f in self.current_findings(now=now)}
        return sorted({f.topic for f in self.findings} - fresh, key=lambda t: t.value)

    def by_topic(
        self, topic: ResearchTopic, *, include_stale: bool = False
    ) -> list[ResearchFinding]:
        pool = self.findings if include_stale else self.current_findings()
        return [f for f in pool if f.topic is topic]


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "CompanyResearch",
    "ResearchFinding",
    "ResearchTopic",
]
