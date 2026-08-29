"""Evidence index over a career profile.

A lightweight, keyword-based retrieval layer over the claim ledger. Deliberately
not a vector store: at the scale of one user's career, deterministic retrieval
is worth more than a fuzzy embedding, and it keeps tailoring testable offline.

The seam where a vector store would later plug in is documented in
:mod:`applyuminati.memory.retrieval`; this module stays exact-match.
"""

from __future__ import annotations

import re
from collections import defaultdict

from applyuminati.core.models.profile import CareerProfile, QuantifiedMetric
from applyuminati.core.provenance import AssertionLevel, Claim

__all__ = ["EvidenceIndex"]

_TOKEN_RE = re.compile(r"[A-Za-z0-9+#.]+")


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text) if len(token) > 1}


class EvidenceIndex:
    """Token index over a profile's claims and metrics."""

    def __init__(self, profile: CareerProfile) -> None:
        self._profile = profile
        self._by_token: dict[str, list[Claim]] = defaultdict(list)
        self._by_id: dict[str, Claim] = {}
        self._metrics_by_claim: dict[str, list[QuantifiedMetric]] = defaultdict(list)
        self._by_employer: dict[str, list[Claim]] = defaultdict(list)

        for claim in profile.claims:
            self._by_id[claim.id] = claim
            for token in _tokens(claim.statement):
                self._by_token[token].append(claim)
            for tag in claim.tags:
                self._by_token[tag].append(claim)
                if tag:
                    self._by_employer[tag].append(claim)

        for metric in profile.metrics:
            if metric.claim_id:
                self._metrics_by_claim[metric.claim_id].append(metric)

    def find(self, term: str) -> list[Claim]:
        """Claims whose statement or tags contain any token of ``term``."""
        hits: dict[str, Claim] = {}
        for token in _tokens(term):
            for claim in self._by_token.get(token, []):
                hits[claim.id] = claim
        return list(hits.values())

    def factual_statements(self) -> set[str]:
        """Lowercased statements of every claim that may be quoted as fact."""
        return {
            claim.statement.lower()
            for claim in self._profile.claims
            if claim.level in (AssertionLevel.VERIFIED, AssertionLevel.USER_APPROVED)
            and claim.superseded_by is None
        }

    def metrics_for(self, claim_id: str) -> list[QuantifiedMetric]:
        return list(self._metrics_by_claim.get(claim_id, []))

    def claims_for_employer(self, name: str) -> list[Claim]:
        return list(self._by_employer.get(name.lower(), []))

    def all_metrics(self) -> list[QuantifiedMetric]:
        return list(self._profile.metrics)

    def claim(self, claim_id: str) -> Claim | None:
        return self._by_id.get(claim_id)
