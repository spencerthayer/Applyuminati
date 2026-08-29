"""Tests for the deterministic scoring engine and application state machine."""

from __future__ import annotations

from applyuminati.applications.idempotency import submission_fingerprint
from applyuminati.applications.machine import ApplicationMachine, IllegalTransitionError
from applyuminati.core.models.application import ActorKind, Application, ApplicationState
from applyuminati.core.models.common import (
    Compensation,
    EmploymentType,
    Location,
    RemoteMode,
    SeniorityLevel,
)
from applyuminati.core.models.job import Job, SourceTier
from applyuminati.core.models.profile import (
    CareerProfile,
    JobTargets,
    WorkAuthorizationStatus,
    WorkEligibility,
)
from applyuminati.core.strategy import SearchStrategy
from applyuminati.scoring.engine import score_job
from applyuminati.sources.normalize import build_job


def _profile() -> CareerProfile:
    from applyuminati.resume.importer import import_json_resume

    payload = {
        "basics": {"name": "Test", "label": "Engineer"},
        "work": [
            {"name": "Acme", "position": "Senior Engineer", "highlights": ["Built X with Python"]}
        ],
        "skills": [{"name": "Programming", "keywords": ["python", "sql", "typescript"]}],
    }
    profile, _ = import_json_resume(payload)
    profile.targets = JobTargets(
        titles=["Senior Software Engineer"],
        seniority=SeniorityLevel.SENIOR,
        remote_modes=[RemoteMode.REMOTE],
        employment_types=[EmploymentType.FULL_TIME],
        compensation_floor=Compensation(minimum=100_000, currency="USD"),
    )
    return profile


def _job(**overrides) -> Job:
    defaults = {
        "source": "test",
        "tier": SourceTier.DIRECT_ATS,
        "source_job_id": "1",
        "url": "https://example.com/1",
        "title": "Senior Software Engineer",
        "company": "Acme",
        "description": "We need Python and SQL engineers.",
        "locations": [Location(raw="Remote")],
        "remote_mode": RemoteMode.REMOTE,
    }
    return build_job(**{**defaults, **overrides})


def test_scoring_produces_apply_for_strong_match() -> None:
    score = score_job(_job(), _profile(), SearchStrategy())
    assert score.overall >= 0.55
    assert score.recommendation.value == "apply"
    assert len(score.dimensions) == 11


def test_scoring_produces_skip_for_hard_blocker() -> None:
    # No work authorization in the job's country.
    profile = _profile()
    profile.eligibility = WorkEligibility(
        authorization={"XX": WorkAuthorizationStatus.NOT_AUTHORIZED}
    )
    profile.strategy = SearchStrategy(work_authorization_is_hard_blocker=True)
    job = _job(locations=[Location(raw="Country X", country_code="XX")])
    score = score_job(job, profile, profile.strategy)
    assert score.overall <= 0.25
    assert score.recommendation.value == "skip"
    assert score.has_hard_blocker


def test_scoring_unknown_compensation_is_uncertainty() -> None:
    profile = _profile()
    profile.targets.compensation_floor = Compensation(minimum=150_000, currency="USD")
    job = _job()  # no compensation in the posting
    score = score_job(job, profile, profile.strategy)
    assert any("compensation" in u.lower() for u in score.uncertainties)


def test_application_transition_legal() -> None:
    app = Application(job_id="j1", profile_id="p1")
    machine = ApplicationMachine()
    machine.transition(app, ApplicationState.SHORTLISTED, actor=ActorKind.SYSTEM, reason="test")
    assert app.state == ApplicationState.SHORTLISTED
    assert len(app.events) == 1


def test_application_transition_illegal_raises() -> None:
    app = Application(job_id="j1", profile_id="p1")
    machine = ApplicationMachine()
    machine.transition(app, ApplicationState.SHORTLISTED, actor=ActorKind.SYSTEM, reason="test")
    try:
        machine.transition(app, ApplicationState.OFFER, actor=ActorKind.USER, reason="test")
        raise AssertionError("should have raised")
    except IllegalTransitionError:
        pass


def test_application_replay_rebuilds_state() -> None:
    app = Application(job_id="j1", profile_id="p1")
    machine = ApplicationMachine()
    for state in [ApplicationState.SHORTLISTED, ApplicationState.PREPARING, ApplicationState.READY]:
        machine.transition(app, state, actor=ActorKind.SYSTEM, reason="pipeline")
    assert ApplicationMachine.replay(app.events) == ApplicationState.READY


def test_submission_fingerprint_is_stable_across_urls() -> None:
    job1 = _job(url="https://a.com/1", source_job_id="a1")
    job2 = _job(url="https://b.com/2", source_job_id="b2")
    fp1 = submission_fingerprint("p1", job1)
    fp2 = submission_fingerprint("p1", job2)
    assert fp1 == fp2  # same company+title+location → same fingerprint
