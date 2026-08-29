"""Deterministic scoring dimensions.

One pure function per :class:`ScoreDimension`. Each returns a
:class:`DimensionScore` with a 0..1 score, a confidence, a rationale a human
can read, and the claim ids that justify it. No I/O, no LLM — the whole file
is testable in isolation and reproducible across runs.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from applyuminati.core.models.common import seniority_distance
from applyuminati.core.models.job import Job
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.scoring import (
    BlockerSeverity,
    DimensionScore,
    MatchedEvidence,
    MissingRequirement,
    ScoreDimension,
)
from applyuminati.core.strategy import RemotePreference, SearchStrategy, Strictness

__all__ = ["score_dimensions"]


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9+#.]+", text) if len(token) > 1}


def _title_match(job: Job, profile: CareerProfile, strategy: SearchStrategy) -> DimensionScore:
    job_tokens = _tokens(job.title)
    if not job_tokens or not profile.targets.titles:
        return DimensionScore(
            dimension=ScoreDimension.TITLE_MATCH,
            score=0.5,
            weight=0.0,
            confidence=0.3,
            rationale="no target titles configured",
        )
    best = 0.0
    matched: list[str] = []
    for target in profile.targets.titles:
        target_tokens = _tokens(target)
        if not target_tokens:
            continue
        overlap = len(job_tokens & target_tokens) / max(len(job_tokens | target_tokens), 1)
        if overlap > best:
            best = overlap
            matched = sorted(job_tokens & target_tokens)
    # exploration widens tolerance: a 0.4 overlap scores higher at high bias.
    score = min(1.0, best + strategy.title_exploration * 0.2)
    anti_hit = [anti for anti in profile.targets.anti_titles if anti.lower() in job.title.lower()]
    if anti_hit:
        score *= 0.6
    rationale = f"{round(best * 100)}% title overlap with nearest target"
    if matched:
        rationale += f" (shared terms: {', '.join(matched)})"
    if anti_hit:
        rationale += f"; anti-title matched: {', '.join(anti_hit)}"
    return DimensionScore(
        dimension=ScoreDimension.TITLE_MATCH,
        score=score,
        weight=0.0,
        confidence=0.7,
        rationale=rationale,
    )


def _seniority(job: Job, profile: CareerProfile, strategy: SearchStrategy) -> DimensionScore:
    if profile.targets.seniority.value == "unknown":
        return DimensionScore(
            dimension=ScoreDimension.SENIORITY_MATCH,
            score=0.5,
            weight=0.0,
            confidence=0.3,
            rationale="target seniority not set",
        )
    distance = seniority_distance(job.seniority, profile.targets.seniority)
    if distance is None:
        return DimensionScore(
            dimension=ScoreDimension.SENIORITY_MATCH,
            score=0.5,
            weight=0.0,
            confidence=0.3,
            rationale=f"posting seniority {job.seniority.value} unrecognised",
        )
    if distance == 0:
        return DimensionScore(
            dimension=ScoreDimension.SENIORITY_MATCH,
            score=1.0,
            weight=0.0,
            confidence=0.9,
            rationale="exact seniority match",
        )
    if distance <= strategy.seniority_tolerance_levels:
        decay = 1.0 - (distance / max(strategy.seniority_tolerance_levels + 1, 1)) * 0.5
        return DimensionScore(
            dimension=ScoreDimension.SENIORITY_MATCH,
            score=max(0.0, decay),
            weight=0.0,
            confidence=0.7,
            rationale=f"{distance} level(s) from target seniority",
        )
    return DimensionScore(
        dimension=ScoreDimension.SENIORITY_MATCH,
        score=0.1,
        weight=0.0,
        confidence=0.8,
        rationale=f"{distance} level(s) from target — beyond tolerance",
    )


def _skills(job: Job, profile: CareerProfile) -> tuple[set[str], set[str], int]:
    """Return (matched skill tokens, missing requirement lines, matched line count)."""
    profile_skills = profile.skill_names()
    if not job.requirements:
        return set(), set(), 0
    matched: set[str] = set()
    missing: set[str] = set()
    matched_lines = 0
    for requirement in job.requirements:
        req_tokens = _tokens(requirement)
        hit = {tok for tok in req_tokens if tok in profile_skills}
        if hit:
            matched |= hit
            matched_lines += 1
        else:
            missing.add(requirement.strip()[:120])
    return matched, missing, matched_lines


def _required_skills(
    job: Job, profile: CareerProfile, strategy: SearchStrategy
) -> tuple[DimensionScore, list[MatchedEvidence], list[MissingRequirement]]:
    matched, missing, matched_lines = _skills(job, profile)
    total = len(job.requirements) or 1
    score = matched_lines / total
    evidence = [
        MatchedEvidence(requirement=skill, claim_id=None, excerpt=None, strength=0.8)
        for skill in sorted(matched)
    ]
    requirements = [
        MissingRequirement(
            requirement=req,
            severity=BlockerSeverity.SIGNIFICANT,
            partially_evidenced=False,
            note="not evidenced in profile",
        )
        for req in sorted(missing)
    ]
    rationale = (
        f"{len(matched)} of {len(job.requirements)} requirement lines matched profile skills"
    )
    return (
        DimensionScore(
            dimension=ScoreDimension.REQUIRED_SKILLS,
            score=score,
            weight=0.0,
            confidence=0.6 if job.requirements else 0.3,
            rationale=rationale,
        ),
        evidence,
        requirements,
    )


def _preferred_skills(job: Job, profile: CareerProfile) -> DimensionScore:
    if not job.preferred_qualifications:
        return DimensionScore(
            dimension=ScoreDimension.PREFERRED_SKILLS,
            score=0.5,
            weight=0.0,
            confidence=0.3,
            rationale="no preferred qualifications listed",
        )
    profile_skills = profile.skill_names()
    matched = 0
    for pref in job.preferred_qualifications:
        if any(tok in profile_skills for tok in _tokens(pref)):
            matched += 1
    score = matched / len(job.preferred_qualifications)
    return DimensionScore(
        dimension=ScoreDimension.PREFERRED_SKILLS,
        score=score,
        weight=0.0,
        confidence=0.6,
        rationale=f"{matched} of {len(job.preferred_qualifications)} preferred qualifications met",
    )


def _demonstrated_experience(
    job: Job, profile: CareerProfile, strategy: SearchStrategy
) -> DimensionScore:
    job_skills = {s.lower() for s in job.skills}
    if not job_skills:
        return DimensionScore(
            dimension=ScoreDimension.DEMONSTRATED_EXPERIENCE,
            score=0.5,
            weight=0.0,
            confidence=0.3,
            rationale="no skills extracted from posting",
        )
    factual = [c for c in profile.factual_claims() if c.tags]
    evidence_count = 0
    claim_ids: list[str] = []
    for claim in factual:
        if any(tag.lower() in job_skills for tag in claim.tags):
            evidence_count += 1
            claim_ids.append(claim.id)
    score = min(1.0, evidence_count / max(len(job_skills), 1))
    return DimensionScore(
        dimension=ScoreDimension.DEMONSTRATED_EXPERIENCE,
        score=score,
        weight=0.0,
        confidence=0.7,
        rationale=f"{evidence_count} factual claims tagged with job-relevant skills",
        evidence_claim_ids=claim_ids[:20],
    )


def _domain_overlap(job: Job, profile: CareerProfile) -> DimensionScore:
    profile_industries = {ind.lower() for ind in profile.targets.industries}
    if not profile_industries:
        return DimensionScore(
            dimension=ScoreDimension.DOMAIN_OVERLAP,
            score=0.5,
            weight=0.0,
            confidence=0.3,
            rationale="no target industries configured",
        )
    haystack = f"{job.company} {job.description or ''}".lower()
    hits = [ind for ind in profile_industries if ind in haystack]
    score = min(1.0, len(hits) / len(profile_industries))
    return DimensionScore(
        dimension=ScoreDimension.DOMAIN_OVERLAP,
        score=score,
        weight=0.0,
        confidence=0.5,
        rationale=f"{len(hits)} of {len(profile_industries)} target industries referenced",
    )


def _compensation(
    job: Job, profile: CareerProfile, strategy: SearchStrategy
) -> tuple[DimensionScore, MissingRequirement | None]:
    requirement = profile.targets.compensation_floor
    if requirement is None or not requirement.is_specified:
        return (
            DimensionScore(
                dimension=ScoreDimension.COMPENSATION,
                score=0.5,
                weight=0.0,
                confidence=0.3,
                rationale="no compensation floor configured",
            ),
            None,
        )
    offered = job.compensation
    if offered is None or not offered.is_specified:
        return (
            DimensionScore(
                dimension=ScoreDimension.COMPENSATION,
                score=0.5,
                weight=0.0,
                confidence=0.2,
                rationale="posting does not state compensation; requirement unverified",
            ),
            None,
        )
    satisfies = offered.satisfies(requirement)
    if satisfies is None:
        return (
            DimensionScore(
                dimension=ScoreDimension.COMPENSATION,
                score=0.5,
                weight=0.0,
                confidence=0.2,
                rationale="compensation currency mismatch; requirement unverified",
            ),
            None,
        )
    if satisfies:
        return (
            DimensionScore(
                dimension=ScoreDimension.COMPENSATION,
                score=0.9,
                weight=0.0,
                confidence=0.8,
                rationale="stated compensation meets the configured floor",
            ),
            None,
        )
    blocker = MissingRequirement(
        requirement="compensation below configured floor",
        severity=BlockerSeverity.HARD
        if strategy.compensation_strictness is Strictness.HARD
        else BlockerSeverity.SIGNIFICANT,
        note=(
            f"posting {offered.raw_text or 'range'} vs floor "
            f"{requirement.minimum} {requirement.currency}"
        ),
    )
    return (
        DimensionScore(
            dimension=ScoreDimension.COMPENSATION,
            score=0.1,
            weight=0.0,
            confidence=0.8,
            rationale="stated compensation below the configured floor",
        ),
        blocker,
    )


def _location(job: Job, profile: CareerProfile, strategy: SearchStrategy) -> DimensionScore:
    if not profile.targets.locations:
        return DimensionScore(
            dimension=ScoreDimension.LOCATION,
            score=0.6,
            weight=0.0,
            confidence=0.3,
            rationale="no target locations configured",
        )
    if job.remote_mode.value == "remote" and strategy.remote_preference in (
        RemotePreference.REMOTE_ONLY,
        RemotePreference.REMOTE_PREFERRED,
    ):
        return DimensionScore(
            dimension=ScoreDimension.LOCATION,
            score=1.0,
            weight=0.0,
            confidence=0.8,
            rationale="posting is remote and user prefers remote",
        )
    target_texts = [loc.display().lower() for loc in profile.targets.locations]
    job_texts = [loc.display().lower() for loc in job.locations] or [job.company.lower()]
    best = max(
        (
            SequenceMatcher(None, target, job_text).ratio()
            for target in target_texts
            for job_text in job_texts
        ),
        default=0.0,
    )
    if best >= 0.8:
        score = 1.0
        rationale = "location matches a target"
    elif best >= 0.5:
        score = 0.5
        rationale = "location partially overlaps a target"
    else:
        score = 0.1
        rationale = "location does not match any target"
    if strategy.location_strictness is Strictness.IGNORED:
        score = max(score, 0.5)
        rationale += " (location strictness ignored)"
    return DimensionScore(
        dimension=ScoreDimension.LOCATION,
        score=score,
        weight=0.0,
        confidence=0.6,
        rationale=rationale,
    )


def _employment_type(job: Job, profile: CareerProfile) -> DimensionScore:
    wanted = {et.value for et in profile.targets.employment_types}
    if not wanted or job.employment_type.value == "unknown":
        return DimensionScore(
            dimension=ScoreDimension.EMPLOYMENT_TYPE,
            score=0.5,
            weight=0.0,
            confidence=0.3,
            rationale="employment type unknown or unconstrained",
        )
    score = 1.0 if job.employment_type.value in wanted else 0.1
    return DimensionScore(
        dimension=ScoreDimension.EMPLOYMENT_TYPE,
        score=score,
        weight=0.0,
        confidence=0.7,
        rationale=f"posting is {job.employment_type.value}",
    )


def _work_authorization(
    job: Job, profile: CareerProfile, strategy: SearchStrategy
) -> tuple[DimensionScore, MissingRequirement | None]:
    if not job.locations:
        return (
            DimensionScore(
                dimension=ScoreDimension.WORK_AUTHORIZATION,
                score=0.5,
                weight=0.0,
                confidence=0.3,
                rationale="posting location unknown; authorisation unverified",
            ),
            None,
        )
    blockers: list[MissingRequirement] = []
    worst_score = 1.0
    for loc in job.locations:
        status = profile.eligibility.status_for(loc.country_code)
        if status.value in (
            "citizen",
            "permanent_resident",
            "work_visa",
            "student_visa",
            "working_holiday",
        ):
            continue
        if (
            status.value == "requires_sponsorship"
            and profile.eligibility.requires_sponsorship is False
        ):
            worst_score = min(worst_score, 0.1)
            blockers.append(
                MissingRequirement(
                    requirement=f"work authorisation for {loc.display()}",
                    severity=BlockerSeverity.HARD
                    if strategy.work_authorization_is_hard_blocker
                    else BlockerSeverity.SIGNIFICANT,
                    note="profile requires sponsorship and posting is in this country",
                )
            )
        elif status.value == "not_authorized":
            worst_score = min(worst_score, 0.0)
            blockers.append(
                MissingRequirement(
                    requirement=f"work authorisation for {loc.display()}",
                    severity=BlockerSeverity.HARD
                    if strategy.work_authorization_is_hard_blocker
                    else BlockerSeverity.SIGNIFICANT,
                    note="profile is not authorised to work in this country",
                )
            )
    if not blockers:
        return (
            DimensionScore(
                dimension=ScoreDimension.WORK_AUTHORIZATION,
                score=1.0,
                weight=0.0,
                confidence=0.8,
                rationale="authorisation present for every posting location",
            ),
            None,
        )
    return (
        DimensionScore(
            dimension=ScoreDimension.WORK_AUTHORIZATION,
            score=worst_score,
            weight=0.0,
            confidence=0.8,
            rationale=f"authorisation gap for {len(blockers)} location(s)",
        ),
        blockers[0],
    )


def _user_preference(job: Job, profile: CareerProfile) -> DimensionScore:
    company_key = job.company_key
    if company_key in {c.lower() for c in profile.strategy.excluded_companies}:
        return DimensionScore(
            dimension=ScoreDimension.USER_PREFERENCE,
            score=0.0,
            weight=0.0,
            confidence=0.9,
            rationale="company is on the excluded list",
        )
    if company_key in {c.lower() for c in profile.strategy.preferred_companies}:
        return DimensionScore(
            dimension=ScoreDimension.USER_PREFERENCE,
            score=1.0,
            weight=0.0,
            confidence=0.9,
            rationale="company is on the preferred list",
        )
    industry_hits = [
        ind
        for ind in profile.strategy.preferred_industries
        if ind.lower() in (job.description or "").lower()
    ]
    if industry_hits:
        return DimensionScore(
            dimension=ScoreDimension.USER_PREFERENCE,
            score=0.8,
            weight=0.0,
            confidence=0.6,
            rationale=f"preferred industry referenced: {', '.join(industry_hits[:3])}",
        )
    return DimensionScore(
        dimension=ScoreDimension.USER_PREFERENCE,
        score=0.5,
        weight=0.0,
        confidence=0.4,
        rationale="no explicit preference signal",
    )


def score_dimensions(
    job: Job, profile: CareerProfile, strategy: SearchStrategy
) -> tuple[list[DimensionScore], list[MatchedEvidence], list[MissingRequirement], list[str]]:
    """Run every dimension, returning scores plus their evidence and blockers."""
    req_score, req_evidence, req_missing = _required_skills(job, profile, strategy)
    comp_score, comp_blocker = _compensation(job, profile, strategy)
    auth_score, auth_blocker = _work_authorization(job, profile, strategy)

    dimensions = [
        _title_match(job, profile, strategy),
        _seniority(job, profile, strategy),
        req_score,
        _preferred_skills(job, profile),
        _demonstrated_experience(job, profile, strategy),
        _domain_overlap(job, profile),
        comp_score,
        _location(job, profile, strategy),
        _employment_type(job, profile),
        auth_score,
        _user_preference(job, profile),
    ]
    evidence = req_evidence
    missing = list(req_missing)
    if comp_blocker:
        missing.append(comp_blocker)
    if auth_blocker:
        missing.append(auth_blocker)
    uncertainties: list[str] = []
    if comp_score.confidence < 0.4:
        uncertainties.append("compensation not verifiable from the posting")
    if auth_score.confidence < 0.4:
        uncertainties.append("work authorisation could not be confirmed")
    return dimensions, evidence, missing, uncertainties
