"""Shared job normalisation: every source emits the same canonical shape.

``build_job`` is the single entry point a plugin calls. It does the work that
would otherwise be duplicated across adapters — URL canonicalisation, HTML
stripping, requirement splitting, skill extraction, compensation parsing,
remote-mode inference, payload hashing — so adding a source never means
reimplementing any of it.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from applyuminati.core.clock import ensure_utc
from applyuminati.core.ids import new_ulid
from applyuminati.core.models.common import (
    Compensation,
    CompensationPeriod,
    EmploymentType,
    Location,
    RemoteMode,
)
from applyuminati.core.models.job import AtsVendor, Job, JobSourceRecord, SourceTier
from applyuminati.core.provenance import Confidence
from applyuminati.sources.text import extract_skills, html_to_text, split_requirements

__all__ = ["build_job", "build_source_record", "parse_compensation"]


_REMOTE_KEYWORDS = (
    (re.compile(r"\bremote\b", re.I), RemoteMode.REMOTE),
    (re.compile(r"\bhybrid\b", re.I), RemoteMode.HYBRID),
    (re.compile(r"\bon[- ]?site\b|\bin[- ]?office\b|\bin office\b", re.I), RemoteMode.ONSITE),
)
_REMOTE_LOCATION_RE = re.compile(r"\bremote\b", re.I)

_COMP_PATTERNS: tuple[tuple[re.Pattern[str], CompensationPeriod], ...] = (
    # $120,000 - $150,000 / yr  |  $120k-$150k  |  120000-150000 USD
    (
        re.compile(
            r"\$\s?(\d[\d,]{0,}(?:\.\d+)?)\s?([kKmM])?\s*(?:[--to]+|-|—)\s?\$?\s?"
            r"(\d[\d,]{0,}(?:\.\d+)?)\s?([kKmM])?\s*(?:/?(?:yr|year|annual(?:ly)?))?\b",
            re.I,
        ),
        CompensationPeriod.YEARLY,
    ),
    (
        re.compile(r"\$\s?(\d[\d,]+)\s?(?:[--to]+|-|—)\s?\$?\s?(\d[\d,]+)\b"),
        CompensationPeriod.YEARLY,
    ),
    # single value: $120,000/yr or $120k
    (
        re.compile(r"\$\s?(\d[\d,]{0,}(?:\.\d+)?)\s?([kKmM])\s*(?:/?(?:yr|year|annual(?:ly)?))?\b"),
        CompensationPeriod.YEARLY,
    ),
    (
        re.compile(r"\$\s?(\d[\d,]{3,}(?:\.\d+)?)\s*(?:/?(?:yr|year|annual(?:ly)?))?\b"),
        CompensationPeriod.YEARLY,
    ),
    # hourly: $60/hr, $60/hour
    (re.compile(r"\$\s?(\d[\d.]+)\s?/(?:hr|hour)\b", re.I), CompensationPeriod.HOURLY),
)

_K_SUFFIX = {"k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000}


def _expand_number(raw: str, suffix: str | None) -> float:
    value = float(raw.replace(",", ""))
    if suffix and suffix in _K_SUFFIX:
        value *= _K_SUFFIX[suffix]
    return value


def parse_compensation(text: str | None) -> Compensation | None:
    """Parse a pay range from free text.

    Returns ``None`` for anything ambiguous rather than guessing. Recognises
    annual ranges (``$120k-$150k``, ``$120,000 - $150,000``), single annual
    values, and hourly rates (``$60/hr``). Currency is assumed USD unless an
    ISO code appears in the matched window.
    """
    if not text:
        return None
    for pattern, period in _COMP_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        groups: tuple[str | None, ...] = match.groups()
        currency = "USD"
        code_match = re.search(r"\b(USD|EUR|GBP|CAD|AUD|JPY|INR|SGD)\b", text, re.IGNORECASE)
        if code_match:
            currency = code_match.group(1).upper()
        if len(groups) >= 4 and groups[0] and groups[2]:
            low = _expand_number(groups[0], groups[1])
            high = _expand_number(groups[2], groups[3])
            return Compensation(
                minimum=low, maximum=high, currency=currency, period=period, raw_text=match.group(0)
            )
        if groups and groups[0]:
            value = _expand_number(groups[0], groups[1] if len(groups) > 1 else None)
            return Compensation(
                minimum=value, currency=currency, period=period, raw_text=match.group(0)
            )
    return None


def _infer_remote_mode(
    *,
    remote_hint: RemoteMode | None,
    location_text: str | None,
    description: str | None,
) -> RemoteMode:
    if remote_hint is not None and remote_hint is not RemoteMode.UNKNOWN:
        return remote_hint
    for haystack in (location_text or "", description or ""):
        for pattern, mode in _REMOTE_KEYWORDS:
            if pattern.search(haystack):
                return mode
    if location_text and _REMOTE_LOCATION_RE.search(location_text):
        return RemoteMode.REMOTE
    return RemoteMode.UNKNOWN


_REMOTE_TO_EMPLOYMENT: dict[str, EmploymentType] = {
    "full time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contract-to-hire": EmploymentType.CONTRACT_TO_HIRE,
    "temporary": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
    "apprenticeship": EmploymentType.APPRENTICESHIP,
    "volunteer": EmploymentType.VOLUNTEER,
}


def _infer_employment_type(text: str | None) -> EmploymentType:
    if not text:
        return EmploymentType.UNKNOWN
    lowered = text.lower()
    for key, value in _REMOTE_TO_EMPLOYMENT.items():
        if key in lowered:
            return value
    return EmploymentType.UNKNOWN


def _payload_hash(raw: dict[str, Any]) -> str | None:
    if not raw:
        return None
    try:
        serialised = json.dumps(raw, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.blake2b(serialised.encode("utf-8"), digest_size=16).hexdigest()


def build_source_record(
    *,
    source: str,
    tier: SourceTier,
    source_job_id: str,
    url: str,
    apply_url: str | None = None,
    confidence: Confidence = 0.8,
    raw: dict[str, Any] | None = None,
) -> JobSourceRecord:
    return JobSourceRecord(
        source=source,
        tier=tier,
        source_job_id=source_job_id,
        url=url,
        apply_url=apply_url,
        confidence=confidence,
        payload_hash=_payload_hash(raw or {}),
        raw=raw or {},
    )


def build_job(
    *,
    source: str,
    tier: SourceTier,
    source_job_id: str,
    url: str,
    title: str,
    company: str,
    description: str | None = None,
    locations: list[Location] | None = None,
    compensation: Compensation | None = None,
    compensation_text: str | None = None,
    employment_type: EmploymentType | None = None,
    employment_type_text: str | None = None,
    remote_mode: RemoteMode | None = None,
    posted_at: datetime | None = None,
    apply_url: str | None = None,
    ats: AtsVendor = AtsVendor.UNKNOWN,
    company_domain: str | None = None,
    department: str | None = None,
    raw: dict[str, Any] | None = None,
    skills_vocabulary: set[str] | None = None,
) -> Job:
    """Build a fully-normalised :class:`Job` with one source record attached."""
    clean_description = html_to_text(description) if description else None
    required, preferred = split_requirements(clean_description) if clean_description else ([], [])
    skills = extract_skills(clean_description or "", vocabulary=skills_vocabulary)

    resolved_comp = compensation or parse_compensation(compensation_text or clean_description)
    location_text = locations[0].display() if locations else None
    resolved_remote = _infer_remote_mode(
        remote_hint=remote_mode, location_text=location_text, description=clean_description
    )
    resolved_employment = employment_type or _infer_employment_type(
        employment_type_text or clean_description
    )
    resolved_posted = ensure_utc(posted_at) if posted_at else None

    record = build_source_record(
        source=source,
        tier=tier,
        source_job_id=source_job_id,
        url=url,
        apply_url=apply_url,
        raw=raw,
    )

    return Job(
        id=new_ulid(),
        canonical_url=url,
        apply_url=apply_url,
        company=company,
        company_domain=company_domain,
        department=department,
        title=title,
        locations=locations or [],
        remote_mode=resolved_remote,
        employment_type=resolved_employment,
        compensation=resolved_comp,
        description=clean_description,
        requirements=required,
        preferred_qualifications=preferred,
        skills=skills,
        posted_at=resolved_posted,
        ats=ats,
        sources=[record],
    )
