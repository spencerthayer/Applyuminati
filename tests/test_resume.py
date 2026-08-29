"""Tests for JSON Resume import, export, and the fabrication guard."""

from __future__ import annotations

from applyuminati.resume.exporter import export_json_resume
from applyuminati.resume.guard import FabricationGuard
from applyuminati.resume.importer import import_json_resume


def test_import_creates_verified_claims(sample_resume: dict) -> None:
    profile, warnings = import_json_resume(sample_resume)
    assert warnings == []
    assert len(profile.claims) >= 4  # 1 role + 2 highlights + 1 education
    assert all(c.level.value == "verified" for c in profile.claims)
    # Provenance points back at the source.
    assert all(p[0].kind.value == "resume_import" for p in (c.provenance for c in profile.claims))


def test_import_extracts_metrics(sample_resume: dict) -> None:
    profile, _ = import_json_resume(sample_resume)
    assert len(profile.metrics) >= 1
    metric = profile.metrics[0]
    assert metric.claim_id is not None
    assert metric.value > 0


def test_export_round_trips(sample_resume: dict) -> None:
    profile, _ = import_json_resume(sample_resume)
    exported = export_json_resume(profile)
    re_imported, _ = import_json_resume(exported)
    assert re_imported.resume.basics.name == profile.resume.basics.name
    assert len(re_imported.resume.work) == len(profile.resume.work)


def test_guard_passes_on_truthful_resume(sample_resume: dict) -> None:
    profile, _ = import_json_resume(sample_resume)
    guard = FabricationGuard(profile)
    report = guard.check(profile.resume)
    assert report.ok


def test_guard_catches_invented_metric(sample_resume: dict) -> None:
    profile, _ = import_json_resume(sample_resume)
    guard = FabricationGuard(profile)
    bad = profile.resume.model_copy(
        update={
            "work": [
                profile.resume.work[0].model_copy(update={"highlights": ["Reduced costs by 99%"]})
            ]
        }
    )
    report = guard.check(bad)
    assert not report.ok
    assert any(v.kind == "invented_metric" for v in report.hard_violations)


def test_guard_catches_invented_employer(sample_resume: dict) -> None:
    profile, _ = import_json_resume(sample_resume)
    guard = FabricationGuard(profile)
    from applyuminati.core.models.jsonresume import ResumeWork

    bad = profile.resume.model_copy(
        update={"work": [ResumeWork(name="Fake Company", position="Engineer", highlights=[])]}
    )
    report = guard.check(bad)
    assert not report.ok
    assert any(v.kind == "unknown_employer" for v in report.hard_violations)
