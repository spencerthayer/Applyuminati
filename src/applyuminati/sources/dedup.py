"""Deduplication: one opening, many sources, nothing discarded.

Matching is layered from cheapest to most expensive:

1. exact ``identity_key`` (already computed on every Job);
2. a shared canonical URL across the two jobs' source records;
3. ``(company_key, title_key)`` with a location-compatibility check, using a
   token-set similarity threshold to tolerate minor rewording.

``merge`` keeps every source record, lets the highest-tier observation win
scalar fields, unions requirements and skills, and appends merged ids so the
provenance of a merge is itself inspectable.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from applyuminati.core.models.job import Job, JobSourceRecord, SourceTier
from applyuminati.core.models.job import canonicalize_url

__all__ = ["Deduplicator", "similarity"]

_SIMILARITY_THRESHOLD = 0.86


def similarity(a: Job, b: Job) -> float:
    """Title token-set similarity, bumped when the companies match.

    Uses ``difflib`` rather than a vector model: at the scale of a single
    user's job feed, deterministic similarity is worth more than a fuzzy
    embedding, and it keeps the dedup path testable offline.
    """
    title_score = SequenceMatcher(None, a.title_key, b.title_key).ratio()
    if a.company_key and a.company_key == b.company_key:
        title_score = min(1.0, title_score + 0.15)
    return title_score


def _locations_compatible(a: Job, b: Job) -> bool:
    if not a.locations or not b.locations:
        return True  # unknown location is compatible with anything
    a_text = {loc.display().lower() for loc in a.locations}
    b_text = {loc.display().lower() for loc in b.locations}
    if a_text & b_text:
        return True
    # "Remote" is compatible with any remote/hybrid posting.
    a_remote = a.remote_mode.value in ("remote", "hybrid")
    b_remote = b.remote_mode.value in ("remote", "hybrid")
    return a_remote and b_remote


class Deduplicator:
    """Decides whether two jobs are the same opening and merges them."""

    def key_candidates(self, job: Job) -> list[str]:
        """Keys to look up in priority order."""
        candidates = [job.identity_key]
        candidates.extend(record.canonical_url for record in job.sources if record.canonical_url)
        return candidates

    def is_duplicate(self, existing: Job, incoming: Job) -> bool:
        if existing.identity_key == incoming.identity_key:
            return True
        existing_urls = {record.canonical_url for record in existing.sources}
        incoming_urls = {record.canonical_url for record in incoming.sources}
        if existing_urls & incoming_urls:
            return True
        if (
            existing.company_key
            and existing.company_key == incoming.company_key
            and similarity(existing, incoming) >= _SIMILARITY_THRESHOLD
            and _locations_compatible(existing, incoming)
        ):
            return True
        return False

    def merge(self, existing: Job, incoming: Job) -> Job:
        """Fold ``incoming`` into ``existing``, keeping every observation."""
        # Union source records, deduplicating on (source, source_job_id).
        records: list[JobSourceRecord] = list(existing.sources)
        known = {(record.source, record.source_job_id) for record in records}
        for record in incoming.sources:
            if (record.source, record.source_job_id) not in known:
                records.append(record)
                known.add((record.source, record.source_job_id))

        # Pick the canonical field set from the highest-tier observation.
        best_record = max(records, key=lambda record: record.priority)
        tier_rank = {
            SourceTier.DIRECT_ATS: 3,
            SourceTier.EMPLOYER_SITE: 2,
            SourceTier.AGGREGATOR: 1,
            SourceTier.DERIVED: 0,
        }
        winner = existing if tier_rank[existing.best_tier] >= tier_rank[incoming.best_tier] else incoming

        merged_ids = list(existing.merged_job_ids) + [incoming.id] + list(incoming.merged_job_ids)

        # Union content lists; keep the more specific compensation.
        requirements = list(dict.fromkeys(existing.requirements + incoming.requirements))
        preferred = list(dict.fromkeys(existing.preferred_qualifications + incoming.preferred_qualifications))
        skills = sorted(set(existing.skills) | set(incoming.skills))
        compensation = incoming.compensation or existing.compensation

        return existing.model_copy(
            update={
                "sources": records,
                "title": winner.title,
                "title_raw": winner.title_raw,
                "company": winner.company,
                "company_key": winner.company_key,
                "company_domain": winner.company_domain or incoming.company_domain,
                "department": winner.department or incoming.department,
                "description": winner.description or existing.description,
                "requirements": requirements,
                "preferred_qualifications": preferred,
                "skills": skills,
                "compensation": compensation,
                "apply_url": existing.apply_url or incoming.apply_url,
                "canonical_url": canonicalize_url(best_record.url),
                "discovered_at": min(existing.discovered_at, incoming.discovered_at),
                "last_seen_at": max(existing.last_seen_at, incoming.last_seen_at),
                "posted_at": existing.posted_at or incoming.posted_at,
                "valid_through": existing.valid_through or incoming.valid_through,
                "merged_job_ids": merged_ids,
                "stage": existing.stage,
            }
        )
