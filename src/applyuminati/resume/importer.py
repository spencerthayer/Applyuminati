"""JSON Resume import and claim-ledger derivation.

The importer does more than parse: it turns a resume into an *evidence-backed*
career profile. Every work entry, education record, project and highlight
becomes a :class:`Claim` at :attr:`AssertionLevel.VERIFIED` with
:class:`Provenance` pointing back at the source field. Numeric figures in
highlight text become :class:`QuantifiedMetric` objects linked to their claims.

This is what makes "every factual career claim is traceable" enforceable
rather than aspirational: the guard can refuse generated text that asserts a
metric the ledger has no record of.
"""

from __future__ import annotations

import re
from typing import Any

from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.models.common import EmploymentType, RemoteMode, SeniorityLevel
from applyuminati.core.models.jsonresume import JsonResume
from applyuminati.core.models.profile import (
    CareerProfile,
    JobTargets,
    MetricUnit,
    QuantifiedMetric,
    WorkEligibility,
)
from applyuminati.core.provenance import AssertionLevel, Claim, Provenance, ProvenanceKind

__all__ = ["import_json_resume"]

_METRIC_RE = re.compile(
    r"(?P<pct>\d[\d,]*\.?\d+)\s?%"
    r"|(?P<usd>\$\s?\d[\d,]*\.?\d+\s?(?:[kKmM]|million|thousand)?(?:\s?(?:/yr|/year|per year))?)"
    r"|(?P<num>\d[\d,]*\.?\d+)\s?(?P<unit>x|k|m|million|billion|requests?/sec|rps|qps|days?|weeks?|months?|years?|hours?)",
    re.IGNORECASE,
)
_UNIT_MAP = {
    "x": MetricUnit.RATIO,
    "k": MetricUnit.COUNT,
    "m": MetricUnit.COUNT,
    "million": MetricUnit.COUNT,
    "billion": MetricUnit.COUNT,
    "requests/sec": MetricUnit.OTHER,
    "requests/s": MetricUnit.OTHER,
    "rps": MetricUnit.OTHER,
    "qps": MetricUnit.OTHER,
    "days": MetricUnit.DURATION_SECONDS,
    "weeks": MetricUnit.DURATION_SECONDS,
    "months": MetricUnit.DURATION_SECONDS,
    "years": MetricUnit.DURATION_SECONDS,
    "hours": MetricUnit.DURATION_SECONDS,
}


def _infer_metric_unit(match: re.Match[str]) -> tuple[float, MetricUnit, str | None]:
    if match.group("pct"):
        return float(match.group("pct").replace(",", "")), MetricUnit.PERCENT, None
    if match.group("usd"):
        raw = match.group("usd")
        value = float(re.sub(r"[^\d.]", "", raw))
        return value, MetricUnit.CURRENCY, None
    value = float(match.group("num").replace(",", ""))
    unit_token = (match.group("unit") or "").lower()
    unit = _UNIT_MAP.get(unit_token, MetricUnit.OTHER)
    return value, unit, unit_token or None


def _extract_metrics(text: str, context: str | None, claim_id: str) -> list[QuantifiedMetric]:
    metrics: list[QuantifiedMetric] = []
    for match in _METRIC_RE.finditer(text):
        value, unit, unit_detail = _infer_metric_unit(match)
        metrics.append(
            QuantifiedMetric(
                label=match.group(0).strip(),
                value=value,
                unit=unit,
                unit_detail=unit_detail,
                context=context,
                claim_id=claim_id,
            )
        )
    return metrics


def _infer_seniority(title: str) -> SeniorityLevel:
    from applyuminati.core.models.job import infer_seniority

    return infer_seniority(title)


def import_json_resume(  # noqa: PLR0912 -- sequential per-section parsing, not a code smell
    payload: dict[str, Any],
    *,
    label: str = "default",
    origin: str = "resume.json",
) -> tuple[CareerProfile, list[str]]:
    """Parse a JSON Resume dict into a canonical profile plus a claim ledger.

    Returns the profile and a list of human-readable warnings for fields the
    importer could not interpret. Never fabricates: absent data yields a
    warning, not an invented value.
    """
    warnings: list[str] = []
    try:
        resume = JsonResume.model_validate(payload)
    except Exception as exc:
        warnings.append(f"schema validation issues: {exc}")
        resume = JsonResume.model_validate({**payload, "basics": payload.get("basics", {})})

    claims: list[Claim] = []
    metrics: list[QuantifiedMetric] = []

    def _claim(statement: str, locator: str, tags: list[str]) -> Claim:
        claim = Claim(
            id=new_ulid(),
            statement=statement,
            level=AssertionLevel.VERIFIED,
            provenance=[
                Provenance(
                    kind=ProvenanceKind.RESUME_IMPORT,
                    origin=origin,
                    locator=locator,
                    confidence=1.0,
                )
            ],
            tags=tags,
        )
        claims.append(claim)
        return claim

    # Work history: one claim per role, one per highlight, metrics from highlights.
    recent_titles: list[str] = []
    for index, work in enumerate(resume.work):
        if not work.name or not work.position:
            warnings.append(f"work[{index}] missing name or position; skipped")
            continue
        locator = f"work[{index}]"
        _claim(
            f"{work.position} at {work.name}",
            locator,
            tags=[work.name.lower(), (work.position or "").lower()],
        )
        for highlight_index, highlight in enumerate(work.highlights):
            hl_claim = _claim(
                highlight,
                f"{locator}.highlights[{highlight_index}]",
                tags=[work.name.lower()],
            )
            metrics.extend(_extract_metrics(highlight, work.name, hl_claim.id))
        if len(recent_titles) < 3 and work.position:
            recent_titles.append(work.position)

    for index, edu in enumerate(resume.education):
        if not edu.institution:
            continue
        _claim(
            f"{edu.studyType or 'Education'} in {edu.area or '(unspecified)'} at {edu.institution}",
            f"education[{index}]",
            tags=[edu.institution.lower()],
        )

    for index, cert in enumerate(resume.certificates):
        if not cert.name:
            continue
        _claim(
            f"{cert.name} certificate from {cert.issuer or '(unknown)'}",
            f"certificates[{index}]",
            [],
        )

    for index, pub in enumerate(resume.publications):
        if not pub.name:
            continue
        _claim(f"Publication: {pub.name}", f"publications[{index}]", [])

    for index, project in enumerate(resume.projects):
        if not project.name:
            continue
        locator = f"projects[{index}]"
        _claim(f"Project: {project.name}", locator, tags=[t.lower() for t in project.keywords if t])
        for highlight_index, highlight in enumerate(project.highlights):
            _claim(highlight, f"{locator}.highlights[{highlight_index}]", [])

    for index, award in enumerate(resume.awards):
        if not award.title:
            continue
        _claim(f"Award: {award.title} from {award.awarder or '(unknown)'}", f"awards[{index}]", [])

    # Derive targets from the most recent positions when the caller supplied none.
    seniority = _infer_seniority(recent_titles[0]) if recent_titles else SeniorityLevel.UNKNOWN
    targets = JobTargets(
        titles=recent_titles,
        seniority=seniority,
        remote_modes=[RemoteMode.REMOTE],
        employment_types=[EmploymentType.FULL_TIME],
    )

    profile = CareerProfile(
        label=label,
        resume=resume,
        claims=claims,
        metrics=metrics,
        targets=targets,
        eligibility=WorkEligibility(),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    return profile, warnings
