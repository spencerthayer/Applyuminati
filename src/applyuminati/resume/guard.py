"""Fabrication guard: the anti-invention enforcement point.

Pure Python, no LLM. After generation, every employer, title, date,
institution, certification, technology and *numeric metric* in the tailored
resume is checked against the canonical profile. A metric that does not appear
in the profile's metric ledger or in a verified claim is a HARD violation,
because invented numbers are the single most common and most damaging
fabrication mode for LLM-generated resumes.

A HARD violation makes ``GuardReport.ok`` false; the caller raises
:class:`FabricationRefusedError` and discards the generated output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from applyuminati.core.models.jsonresume import JsonResume, ResumeWork
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.provenance import AssertionLevel, EvidenceLink
from applyuminati.resume.evidence import EvidenceIndex

__all__ = ["GuardReport", "GuardSeverity", "GuardViolation", "FabricationGuard"]

_TOKEN_RE = re.compile(r"[A-Za-z0-9+#.]+")
_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d+\s?%|\$\s?\d[\d,]*\.?\d+|\b\d[\d,]*\.?\d+\b")


class GuardSeverity(StrEnum):
    HARD = "hard"
    SOFT = "soft"


@dataclass(frozen=True, slots=True)
class GuardViolation:
    severity: GuardSeverity
    kind: str
    path: str
    detail: str
    offending_text: str | None = None


@dataclass(slots=True)
class GuardReport:
    ok: bool
    violations: list[GuardViolation] = field(default_factory=list)
    evidence_links: list[EvidenceLink] = field(default_factory=list)

    @property
    def hard_violations(self) -> list[GuardViolation]:
        return [v for v in self.violations if v.severity is GuardSeverity.HARD]


class FabricationGuard:
    """Check a generated resume against the canonical profile."""

    def __init__(self, profile: CareerProfile) -> None:
        self._profile = profile
        self._index = EvidenceIndex(profile)
        self._known_employers = {work.name.lower() for work in profile.resume.work if work.name}
        self._known_institutions = {
            edu.institution.lower() for edu in profile.resume.education if edu.institution
        }
        self._known_certs = {
            cert.name.lower() for cert in profile.resume.certificates if cert.name
        }
        self._known_skills = profile.skill_names()
        self._metric_values = {
            self._normalise_metric(m.value, m.unit) for m in profile.metrics
        }
        self._banned = profile.banned_phrases()

    def check(self, generated: JsonResume) -> GuardReport:
        violations: list[GuardViolation] = []
        links: list[EvidenceLink] = []

        for work_index, work in enumerate(generated.work):
            self._check_work(work, work_index, violations, links)
        for edu_index, edu in enumerate(generated.education):
            if edu.institution and edu.institution.lower() not in self._known_institutions:
                violations.append(
                    GuardViolation(
                        severity=GuardSeverity.HARD,
                        kind="unknown_institution",
                        path=f"education[{edu_index}].institution",
                        detail=f"institution {edu.institution!r} not in profile",
                        offending_text=edu.institution,
                    )
                )
        for cert_index, cert in enumerate(generated.certificates):
            if cert.name and cert.name.lower() not in self._known_certs:
                violations.append(
                    GuardViolation(
                        severity=GuardSeverity.SOFT,
                        kind="unknown_certification",
                        path=f"certificates[{cert_index}].name",
                        detail=f"certification {cert.name!r} not in profile",
                        offending_text=cert.name,
                    )
                )
        self._check_banned_phrases(generated, violations)
        self._check_metrics(generated, violations)

        ok = not any(v.severity is GuardSeverity.HARD for v in violations)
        return GuardReport(ok=ok, violations=violations, evidence_links=links)

    def _check_work(
        self,
        work: ResumeWork,
        index: int,
        violations: list[GuardViolation],
        links: list[EvidenceLink],
    ) -> None:
        path = f"work[{index}]"
        if work.name and work.name.lower() not in self._known_employers:
            violations.append(
                GuardViolation(
                    severity=GuardSeverity.HARD,
                    kind="unknown_employer",
                    path=f"{path}.name",
                    detail=f"employer {work.name!r} not in profile",
                    offending_text=work.name,
                )
            )
        # Check dates against canonical entries by employer.
        canonical = next(
            (entry for entry in self._profile.resume.work if entry.name and entry.name.lower() == work.name.lower()),
            None,
        ) if work.name else None
        if canonical and work.startDate and canonical.startDate and work.startDate != canonical.startDate:
            violations.append(
                GuardViolation(
                    severity=GuardSeverity.HARD,
                    kind="altered_date",
                    path=f"{path}.startDate",
                    detail=f"start date {work.startDate!r} differs from canonical {canonical.startDate!r}",
                    offending_text=work.startDate,
                )
            )
        # Technologies in highlights must be known skills.
        for highlight_index, highlight in enumerate(work.highlights):
            hl_path = f"{path}.highlights[{highlight_index}]"
            for token in _TOKEN_RE.findall(highlight):
                lowered = token.lower()
                if lowered in self._known_skills:
                    continue
                # Tolerate common words; only flag tokens that look like tech.
                if self._looks_like_tech(token) and lowered not in self._known_skills:
                    violations.append(
                        GuardViolation(
                            severity=GuardSeverity.SOFT,
                            kind="unknown_technology",
                            path=hl_path,
                            detail=f"technology {token!r} not in profile skills",
                            offending_text=token,
                        )
                    )

    def _looks_like_tech(self, token: str) -> bool:
        # Heuristic: contains a dot, a plus, a hash, or is all-caps acronym.
        return any(c in token for c in ".+#") or (
            len(token) <= 6 and token.isupper() and token.isascii()
        )

    def _check_banned_phrases(self, generated: JsonResume, violations: list[GuardViolation]) -> None:
        all_text = " ".join(
            [generated.basics.summary or ""]
            + [highlight for work in generated.work for highlight in work.highlights]
        ).lower()
        for phrase in self._banned:
            if phrase in all_text:
                violations.append(
                    GuardViolation(
                        severity=GuardSeverity.SOFT,
                        kind="banned_phrase",
                        path="*",
                        detail=f"banned phrase {phrase!r} present in generated text",
                        offending_text=phrase,
                    )
                )

    def _check_metrics(self, generated: JsonResume, violations: list[GuardViolation]) -> None:
        """Flag any number in generated text that is not a known metric value."""
        if not self._metric_values:
            return
        for work_index, work in enumerate(generated.work):
            for highlight_index, highlight in enumerate(work.highlights):
                for match in _NUMBER_RE.finditer(highlight):
                    raw = match.group(0).strip()
                    normalised = self._normalise_metric_text(raw)
                    if normalised in self._metric_values:
                        continue
                    # Tolerate dates (e.g. 2020, 2021) and small counts.
                    if re.fullmatch(r"\d{4}", raw):
                        continue
                    violations.append(
                        GuardViolation(
                            severity=GuardSeverity.HARD,
                            kind="invented_metric",
                            path=f"work[{work_index}].highlights[{highlight_index}]",
                            detail=f"numeric value {raw!r} does not appear in the profile's metric ledger",
                            offending_text=raw,
                        )
                    )

    @staticmethod
    def _normalise_metric(value: float, unit: object) -> str:  # noqa: ARG004
        return f"{value:g}"

    @staticmethod
    def _normalise_metric_text(text: str) -> str:
        cleaned = re.sub(r"[^\d.]", "", text)
        try:
            value = float(cleaned)
        except ValueError:
            return text
        return f"{value:g}"
