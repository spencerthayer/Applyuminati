"""The canonical job posting.

One real-world opening frequently appears through several channels: the
employer's Greenhouse board, an aggregator's copy, and a job-alert email. The
model here keeps **one** :class:`Job` per opening and **every**
:class:`JobSourceRecord` that produced it, so nothing is silently discarded
and the user can always see where a posting came from.

Two rules follow from that:

* Normalisation is additive. ``title_raw`` is kept alongside ``title``, so a
  cleaning bug is recoverable and never destroys the original.
* Absence of evidence is not evidence of absence. A posting that has not been
  re-checked is ``UNVERIFIED``, not ``LIVE`` and not ``GONE``.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid, stable_id
from applyuminati.core.models.common import (
    Compensation,
    EmploymentType,
    Location,
    RemoteMode,
    SeniorityLevel,
)
from applyuminati.core.provenance import Confidence


class AtsVendor(StrEnum):
    """Applicant tracking system behind the apply flow.

    Drives which application workflow hints and per-ATS knowledge pack apply.
    """

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    SMARTRECRUITERS = "smartrecruiters"
    ICIMS = "icims"
    TALEO = "taleo"
    SUCCESSFACTORS = "successfactors"
    JOBVITE = "jobvite"
    BAMBOOHR = "bamboohr"
    RECRUITEE = "recruitee"
    WORKABLE = "workable"
    TEAMTAILOR = "teamtailor"
    EIGHTFOLD = "eightfold"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class SourceTier(StrEnum):
    """How close a source is to the employer.

    Used when merging duplicates: a direct ATS record beats an aggregator copy
    for canonical field values, even when the aggregator was seen first.
    """

    #: Employer's own careers page or their ATS's public API.
    DIRECT_ATS = "direct_ats"
    #: Employer-operated site that is not a recognised ATS.
    EMPLOYER_SITE = "employer_site"
    #: Third-party board that republishes postings.
    AGGREGATOR = "aggregator"
    #: Search engine result, job-alert email, or user-supplied feed.
    DERIVED = "derived"


TIER_PRIORITY: dict[SourceTier, int] = {
    SourceTier.DIRECT_ATS: 3,
    SourceTier.EMPLOYER_SITE: 2,
    SourceTier.AGGREGATOR: 1,
    SourceTier.DERIVED: 0,
}


class VerificationState(StrEnum):
    """Whether the posting is still real.

    ``UNVERIFIED`` is the honest default and is never conflated with ``LIVE``.
    """

    UNVERIFIED = "unverified"
    LIVE = "live"
    #: Fetched successfully but the page says the role is closed/filled.
    CLOSED = "closed"
    #: The URL 404/410s.
    GONE = "gone"
    #: The check itself failed (blocked, timeout); we learned nothing.
    UNKNOWN = "unknown"


class PipelineStage(StrEnum):
    """Where a job is in the processing pipeline. Enables resumable runs."""

    DISCOVERED = "discovered"
    NORMALIZED = "normalized"
    DEDUPLICATED = "deduplicated"
    VERIFIED = "verified"
    RESEARCHED = "researched"
    SCORED = "scored"
    #: A stage failed; ``Job`` carries the failure and the run can resume here.
    FAILED = "failed"


_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gh_src",
        "gh_jid",
        "lever-source",
        "src",
        "ref",
        "source",
        "trk",
        "trackingId",
        "refId",
        "recommendedFlavor",
    }
)

_WS_RE = re.compile(r"\s+")
_TITLE_NOISE_RE = re.compile(
    r"""
    ^\s*(?:\#?\d+\s*[-–—]\s*)          # leading requisition numbers
    | \s*\((?:remote|hybrid|on-?site|full[- ]time|part[- ]time|contract)\)\s*$
    | \s*[-–—|]\s*(?:remote|hybrid|on-?site)\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SENIORITY_PATTERNS: tuple[tuple[re.Pattern[str], SeniorityLevel], ...] = (
    (re.compile(r"\b(intern|internship)\b", re.I), SeniorityLevel.INTERN),
    (re.compile(r"\b(new ?grad|graduate|entry[- ]level)\b", re.I), SeniorityLevel.ENTRY),
    (re.compile(r"\b(junior|jr\.?|associate)\b", re.I), SeniorityLevel.JUNIOR),
    (re.compile(r"\b(vp|vice president)\b", re.I), SeniorityLevel.VP),
    (re.compile(r"\b(cto|ceo|cpo|chief)\b", re.I), SeniorityLevel.EXECUTIVE),
    (re.compile(r"\bdirector\b", re.I), SeniorityLevel.DIRECTOR),
    (re.compile(r"\b(engineering manager|manager|head of)\b", re.I), SeniorityLevel.MANAGER),
    (re.compile(r"\bprincipal\b", re.I), SeniorityLevel.PRINCIPAL),
    (re.compile(r"\bstaff\b", re.I), SeniorityLevel.STAFF),
    (re.compile(r"\b(lead|tech lead)\b", re.I), SeniorityLevel.LEAD),
    (re.compile(r"\b(senior|sr\.?)\b", re.I), SeniorityLevel.SENIOR),
    (re.compile(r"\b(mid[- ]level|ii|iii)\b", re.I), SeniorityLevel.MID),
)


def canonicalize_url(url: str) -> str:
    """Strip tracking noise so the same posting yields the same URL.

    Lowercases scheme/host, drops the fragment, removes known tracking query
    parameters, sorts the survivors, and trims a trailing slash. Deliberately
    conservative: unknown query parameters are preserved because some ATSes
    encode the job id there.
    """
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def normalize_company(name: str) -> str:
    """Fold a company name to a comparison key (``Acme, Inc.`` -> ``acme``)."""
    lowered = _WS_RE.sub(" ", name.strip().lower())
    lowered = re.sub(r"[.,]", "", lowered)
    lowered = re.sub(
        r"\b(inc|llc|ltd|limited|corp|corporation|gmbh|bv|nv|sa|ag|plc|co|company|group|holdings)\b",
        "",
        lowered,
    )
    return _WS_RE.sub(" ", lowered).strip(" -&")


def normalize_title(title: str) -> str:
    """Remove requisition numbers and trailing modality suffixes from a title."""
    cleaned = _TITLE_NOISE_RE.sub("", title.strip())
    return _WS_RE.sub(" ", cleaned).strip(" -–—|")


def title_comparison_key(title: str) -> str:
    """Aggressively folded title, used only for duplicate detection."""
    cleaned = normalize_title(title).lower()
    cleaned = re.sub(r"[^a-z0-9+#. ]", " ", cleaned)
    tokens = [t for t in cleaned.split() if t not in {"the", "a", "an", "of", "for", "and"}]
    return " ".join(sorted(tokens))


def infer_seniority(title: str) -> SeniorityLevel:
    """Best-effort seniority from a title. Returns ``UNKNOWN`` rather than guessing."""
    for pattern, level in _SENIORITY_PATTERNS:
        if pattern.search(title):
            return level
    return SeniorityLevel.UNKNOWN


class JobSourceRecord(BaseModel):
    """Evidence that one source saw this posting.

    Retained for every source that reports the job, even after deduplication.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: Registry slug of the plugin, e.g. ``greenhouse``.
    source: str
    tier: SourceTier = SourceTier.AGGREGATOR
    #: The identifier the source itself uses.
    source_job_id: str
    #: URL exactly as the source gave it.
    url: str
    #: :func:`canonicalize_url` applied to :attr:`url`.
    canonical_url: str = ""
    #: URL to start the application at, when it differs from the posting URL.
    apply_url: str | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    #: How much this source's field values are trusted during merge.
    confidence: Confidence = 0.8
    #: Digest of the source payload, so a changed posting is detectable.
    payload_hash: str | None = None
    #: The untouched payload. Bounded by the plugin, never logged.
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fill_canonical(self) -> Self:
        if not self.canonical_url and self.url:
            object.__setattr__(self, "canonical_url", canonicalize_url(self.url))
        return self

    @property
    def priority(self) -> tuple[int, float]:
        """Merge precedence: tier first, then the source's own confidence."""
        return (TIER_PRIORITY[self.tier], self.confidence)


class Job(BaseModel):
    """One real-world opening, however many sources reported it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)

    # -- identity ---------------------------------------------------------
    #: Deterministic dedup key. See :meth:`compute_identity_key`.
    identity_key: str = ""
    canonical_url: str
    apply_url: str | None = None

    # -- employer & role --------------------------------------------------
    company: str
    company_key: str = ""
    company_domain: str | None = None
    title: str
    title_raw: str = ""
    title_key: str = ""
    department: str | None = None
    seniority: SeniorityLevel = SeniorityLevel.UNKNOWN

    # -- placement --------------------------------------------------------
    locations: list[Location] = Field(default_factory=list)
    remote_mode: RemoteMode = RemoteMode.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    compensation: Compensation | None = None

    # -- content ----------------------------------------------------------
    #: Plain-text description. HTML is stripped at normalisation time.
    description: str | None = None
    #: Extracted "must have" lines.
    requirements: list[str] = Field(default_factory=list)
    #: Extracted "nice to have" lines.
    preferred_qualifications: list[str] = Field(default_factory=list)
    #: Skill tokens mentioned anywhere in the posting.
    skills: list[str] = Field(default_factory=list)

    # -- lifecycle --------------------------------------------------------
    posted_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    last_verified_at: datetime | None = None
    #: Employer-declared expiry, where a source provides one.
    valid_through: datetime | None = None
    verification: VerificationState = VerificationState.UNVERIFIED
    stage: PipelineStage = PipelineStage.DISCOVERED

    # -- provenance -------------------------------------------------------
    ats: AtsVendor = AtsVendor.UNKNOWN
    sources: list[JobSourceRecord] = Field(default_factory=list)
    #: Ids of jobs merged into this one. Nothing is deleted on merge.
    merged_job_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_keys(self) -> Self:
        if not self.title_raw:
            object.__setattr__(self, "title_raw", self.title)
        object.__setattr__(self, "title", normalize_title(self.title))
        object.__setattr__(self, "company_key", normalize_company(self.company))
        object.__setattr__(self, "title_key", title_comparison_key(self.title))
        object.__setattr__(self, "canonical_url", canonicalize_url(self.canonical_url))
        if self.seniority is SeniorityLevel.UNKNOWN:
            object.__setattr__(self, "seniority", infer_seniority(self.title_raw))
        if not self.identity_key:
            object.__setattr__(self, "identity_key", self.compute_identity_key())
        return self

    def compute_identity_key(self) -> str:
        """Deterministic key for "same opening, seen again".

        Built from company + folded title + primary location, *not* from the
        URL: the URL differs between an ATS record and an aggregator copy of
        the same job, which is exactly the case deduplication must catch.
        """
        location = self.locations[0].display().lower() if self.locations else ""
        return stable_id("job", self.company_key, self.title_key, location)

    # -- provenance helpers ----------------------------------------------

    @property
    def primary_source(self) -> JobSourceRecord | None:
        """Highest-priority source record: closest to the employer."""
        return max(self.sources, key=lambda s: s.priority, default=None)

    @property
    def source_slugs(self) -> list[str]:
        return sorted({s.source for s in self.sources})

    @property
    def best_tier(self) -> SourceTier:
        primary = self.primary_source
        return primary.tier if primary else SourceTier.DERIVED

    def source_urls(self) -> set[str]:
        return {s.canonical_url for s in self.sources if s.canonical_url}

    def has_source(self, slug: str, source_job_id: str) -> bool:
        return any(s.source == slug and s.source_job_id == source_job_id for s in self.sources)

    @property
    def is_expired(self) -> bool:
        """True only when we have positive evidence the posting is over."""
        if self.verification in (VerificationState.CLOSED, VerificationState.GONE):
            return True
        return bool(self.valid_through and self.valid_through < utcnow())

    def age_days(self) -> float | None:
        if self.posted_at is None:
            return None
        return (utcnow() - self.posted_at).total_seconds() / 86400.0

    def freshness_days(self) -> float:
        """Days since the posting was last observed by any source."""
        return (utcnow() - self.last_seen_at).total_seconds() / 86400.0


__all__ = [
    "TIER_PRIORITY",
    "AtsVendor",
    "Job",
    "JobSourceRecord",
    "PipelineStage",
    "SourceTier",
    "VerificationState",
    "canonicalize_url",
    "infer_seniority",
    "normalize_company",
    "normalize_title",
    "title_comparison_key",
]
