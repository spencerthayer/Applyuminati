"""ATS detection and Greenhouse/Lever drivers against a fake session."""

from __future__ import annotations

import contextlib
from pathlib import Path

from applyuminati.applications.detect import detect_ats, detect_job
from applyuminati.applications.driver import DriverContext, DriverOutcomeKind, detect_driver
from applyuminati.browser.base import (
    ActionResult,
    BrowserCheckpoint,
    ControlOwner,
    ElementRole,
    PageCondition,
    PageElement,
    PageObservation,
)
from applyuminati.core.clock import utcnow
from applyuminati.core.models.execution import (
    ApplicationAttempt,
    CheckpointKind,
    InterventionReason,
    SubmissionCertainty,
    WorkflowState,
)
from applyuminati.core.models.job import AtsVendor, SourceTier
from applyuminati.core.models.profile import CareerProfile, QuestionnaireDefault
from applyuminati.core.models.questionnaire import ApplicationQuestion, QuestionKind
from applyuminati.core.provenance import AssertionLevel
from applyuminati.core.settings import ExecutionMode
from applyuminati.db.repositories.attempts import AttemptRepository
from applyuminati.plugins.applications import register_application_drivers
from applyuminati.plugins.applications.greenhouse import GreenhouseDriver
from applyuminati.plugins.applications.lever import LeverDriver
from applyuminati.services.container import Repositories
from applyuminati.sources.normalize import build_job


class FakeSession:
    def __init__(self, pages: dict[str, PageObservation], *, start: str = "") -> None:
        self.session_id = "s1"
        self._owner = ControlOwner.AGENT
        self._pages = pages
        self._url = start or next(iter(pages))
        self.clicks: list[str] = []
        self.idempotency_keys: list[str | None] = []

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    async def navigate(self, url: str, *, wait_for_load: bool = True) -> PageObservation:
        self._url = url
        return self._pages.get(url, next(iter(self._pages.values())))

    async def observe(self, *, include_text: bool = True) -> PageObservation:
        return self._pages.get(self._url, next(iter(self._pages.values())))

    async def find_controls(self, *, role: ElementRole | None = None) -> list[PageElement]:
        return (await self.observe()).elements

    async def fill_field(self, locator: str, value: str) -> ActionResult:
        return ActionResult(ok=True, action="fill")

    async def select_option(self, locator: str, option: str) -> ActionResult:
        return ActionResult(ok=True, action="select")

    async def set_checked(self, locator: str, checked: bool) -> ActionResult:
        return ActionResult(ok=True, action="check")

    async def upload_file(self, locator: str, path: Path) -> ActionResult:
        return ActionResult(ok=True, action="upload")

    async def click(
        self,
        locator: str,
        *,
        label: str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        self.clicks.append(locator)
        self.idempotency_keys.append(idempotency_key)
        thanks = next(
            (page for page in self._pages.values() if "thank" in (page.text or "").lower()), None
        )
        if thanks is not None:
            self._url = thanks.url
        return ActionResult(ok=True, action="click")

    async def wait_for_navigation(self, *, timeout_seconds: float | None = None) -> ActionResult:
        return ActionResult(ok=True, action="wait")

    async def screenshot(self, *, relative_path: str) -> str:
        return relative_path

    async def checkpoint(self) -> BrowserCheckpoint:
        return BrowserCheckpoint(session_id=self.session_id, url=self._url)

    async def request_human_control(self, instruction: str) -> ActionResult:
        self._owner = ControlOwner.DELEGATED_TO_USER
        return ActionResult(ok=True, action="handoff")

    async def control_state(self) -> ControlOwner:
        return self._owner

    async def wait_for_control(self, *, timeout_seconds: float) -> ActionResult:
        return ActionResult(ok=self._owner is ControlOwner.AGENT, action="wait")

    async def reclaim_control(self, *, confirmed_by_user: bool) -> ActionResult:
        if not confirmed_by_user:
            return ActionResult(ok=False, action="reclaim", detail="not confirmed")
        self._owner = ControlOwner.AGENT
        return ActionResult(ok=True, action="reclaim")

    async def close(self) -> None:
        return None


def test_application_url_not_discovery_source_selects_the_driver() -> None:
    job = build_job(
        source="linkedin",
        tier=SourceTier.AGGREGATOR,
        source_job_id="1",
        url="https://www.linkedin.com/jobs/view/1",
        title="Engineer",
        company="Acme",
        apply_url="https://jobs.lever.co/acme/abcd",
    )
    assert detect_job(job).ats is AtsVendor.LEVER
    assert detect_ats("https://company.wd5.myworkdayjobs.com/en-US/job").ats is AtsVendor.WORKDAY
    # Longest suffix wins so job-boards.greenhouse.io is not boards.greenhouse.io.
    assert detect_ats("https://job-boards.greenhouse.io/acme/jobs/1").ats is AtsVendor.GREENHOUSE


def test_registered_drivers_are_selected_by_url() -> None:
    register_application_drivers()
    greenhouse = detect_driver("https://boards.greenhouse.io/acme/jobs/1")
    lever = detect_driver("https://jobs.lever.co/acme/abcd")
    assert greenhouse is not None
    assert greenhouse[0].metadata.slug == "greenhouse"
    assert lever is not None
    assert lever[0].metadata.slug == "lever"


async def test_greenhouse_hands_off_a_login_wall() -> None:
    page = PageObservation(
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Sign in",
        condition=PageCondition.LOGIN_REQUIRED,
        text="Please log in",
    )
    session = FakeSession({page.url: page})
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="greenhouse")
    job = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url=page.url,
        title="Engineer",
        company="Acme",
    )
    outcome = await GreenhouseDriver().run(
        attempt,
        session,
        DriverContext(job=job, profile=CareerProfile(), mode=ExecutionMode.FILL_NO_SUBMIT),
    )
    assert outcome.kind is DriverOutcomeKind.WAITING_FOR_HUMAN
    assert outcome.intervention is not None
    assert outcome.intervention.reason is InterventionReason.AUTHENTICATION_REQUIRED
    assert attempt.latest_checkpoint is not None


async def test_greenhouse_records_submission_evidence(tmp_path: Path) -> None:
    apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    form = PageObservation(
        url=apply_url,
        title="Apply",
        text="Application form",
        elements=[
            PageElement(locator="submit", role=ElementRole.BUTTON, label="Submit application"),
            PageElement(locator="resume", role=ElementRole.FILE_INPUT, label="Resume"),
        ],
        questions=[
            ApplicationQuestion(
                text="Full name",
                kind=QuestionKind.SHORT_TEXT,
                field_locator="name",
                key="full_name",
            )
        ],
    )
    thanks = PageObservation(
        url=apply_url + "/thanks",
        title="Thanks",
        text="Thank you for applying. We received your application.",
    )
    session = FakeSession({apply_url: form, thanks.url: thanks})
    profile = CareerProfile(
        questionnaire_defaults=[
            QuestionnaireDefault(
                key="full_name",
                question_text="Full name",
                answer="Jane Engineer",
                level=AssertionLevel.VERIFIED,
            )
        ]
    )
    job = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url=apply_url,
        title="Engineer",
        company="Acme",
    )
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="greenhouse")
    outcome = await GreenhouseDriver().run(
        attempt,
        session,
        DriverContext(
            job=job,
            profile=profile,
            mode=ExecutionMode.AUTONOMOUS_SUBMIT,
            documents={"resume": resume},
            observation=form,
        ),
    )
    assert outcome.kind is DriverOutcomeKind.COMPLETED
    assert attempt.evidence.certainty.value in {"confirmed", "likely"}
    assert attempt.submission_attempted_at is not None
    assert session.clicks == ["submit"]


async def test_lever_does_not_need_greenhouse_core_logic() -> None:
    apply_url = "https://jobs.lever.co/acme/abcd/apply"
    form = PageObservation(
        url=apply_url,
        title="Apply",
        text="Lever application",
        elements=[PageElement(locator="go", role=ElementRole.BUTTON, label="Submit application")],
    )
    thanks = PageObservation(
        url=apply_url + "/thanks", title="Done", text="Application received. Thank you."
    )
    session = FakeSession({apply_url: form, thanks.url: thanks})
    job = build_job(
        source="indeed",
        tier=SourceTier.AGGREGATOR,
        source_job_id="x",
        url="https://www.indeed.com/viewjob?jk=x",
        title="Engineer",
        company="Acme",
        apply_url="https://jobs.lever.co/acme/abcd",
    )
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="lever")
    outcome = await LeverDriver().run(
        attempt,
        session,
        DriverContext(job=job, profile=CareerProfile(), mode=ExecutionMode.AUTONOMOUS_SUBMIT),
    )
    assert outcome.kind is DriverOutcomeKind.COMPLETED
    assert attempt.driver == "lever"
    assert detect_ats(job.apply_url or "").ats is AtsVendor.LEVER


async def test_fill_without_submit_never_clicks_submit() -> None:
    apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    form = PageObservation(
        url=apply_url,
        title="Apply",
        text="form",
        elements=[PageElement(locator="submit", role=ElementRole.BUTTON, label="Submit")],
    )
    session = FakeSession({apply_url: form})
    job = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url=apply_url,
        title="Engineer",
        company="Acme",
    )
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="greenhouse")
    outcome = await GreenhouseDriver().run(
        attempt,
        session,
        DriverContext(job=job, profile=CareerProfile(), mode=ExecutionMode.FILL_NO_SUBMIT),
    )
    assert outcome.kind is DriverOutcomeKind.COMPLETED
    assert session.clicks == []
    assert attempt.submission_attempted_at is None


def _greenhouse_form(apply_url: str) -> PageObservation:
    return PageObservation(
        url=apply_url,
        title="Apply",
        text="Application form",
        elements=[
            PageElement(locator="submit", role=ElementRole.BUTTON, label="Submit application")
        ],
    )


async def test_uncertain_evidence_after_click_opens_user_review() -> None:
    apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    form = _greenhouse_form(apply_url)
    session = FakeSession({apply_url: form})
    job = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url=apply_url,
        title="Engineer",
        company="Acme",
    )
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="greenhouse")
    outcome = await GreenhouseDriver().run(
        attempt,
        session,
        DriverContext(job=job, profile=CareerProfile(), mode=ExecutionMode.AUTONOMOUS_SUBMIT),
    )
    assert outcome.kind is DriverOutcomeKind.WAITING_FOR_HUMAN
    assert outcome.intervention is not None
    assert outcome.intervention.reason is InterventionReason.USER_REVIEW
    assert attempt.workflow_state is WorkflowState.WAITING_FOR_HUMAN
    assert attempt.submission_attempted_at is not None
    assert attempt.evidence.certainty is SubmissionCertainty.UNCERTAIN
    assert not any(
        checkpoint.kind == CheckpointKind.SUBMISSION_CONFIRMED.value
        for checkpoint in attempt.checkpoints
    )
    assert session.clicks == ["submit"]
    assert session.idempotency_keys == [f"application-attempt:{attempt.id}:submit"]


async def test_uncertain_evidence_on_restart_does_not_click_again() -> None:
    apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    form = _greenhouse_form(apply_url)
    session = FakeSession({apply_url: form})
    job = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url=apply_url,
        title="Engineer",
        company="Acme",
    )
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="greenhouse")
    attempt.submission_attempted_at = utcnow()
    outcome = await GreenhouseDriver().run(
        attempt,
        session,
        DriverContext(job=job, profile=CareerProfile(), mode=ExecutionMode.AUTONOMOUS_SUBMIT),
    )
    assert outcome.kind is DriverOutcomeKind.WAITING_FOR_HUMAN
    assert outcome.intervention is not None
    assert outcome.intervention.reason is InterventionReason.USER_REVIEW
    assert attempt.workflow_state is WorkflowState.WAITING_FOR_HUMAN
    assert attempt.submission_attempted_at is not None
    assert session.clicks == []
    assert not any(
        checkpoint.kind == CheckpointKind.SUBMISSION_CONFIRMED.value
        for checkpoint in attempt.checkpoints
    )


async def test_run_refuses_to_act_while_the_user_owns_the_browser() -> None:
    apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    form = _greenhouse_form(apply_url)
    session = FakeSession({apply_url: form})
    session._owner = ControlOwner.DELEGATED_TO_USER
    job = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url=apply_url,
        title="Engineer",
        company="Acme",
    )
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="greenhouse")
    outcome = await GreenhouseDriver().run(
        attempt,
        session,
        DriverContext(job=job, profile=CareerProfile(), mode=ExecutionMode.AUTONOMOUS_SUBMIT),
    )
    assert outcome.kind is DriverOutcomeKind.WAITING_FOR_HUMAN
    assert outcome.intervention is not None
    assert outcome.intervention.reason is InterventionReason.UNKNOWN_INTERACTION
    assert outcome.intervention.requires_browser_handoff is True
    assert attempt.workflow_state is WorkflowState.WAITING_FOR_HUMAN
    assert session.clicks == []
    assert not any(
        checkpoint.kind == CheckpointKind.APPLICATION_OPENED.value
        for checkpoint in attempt.checkpoints
    )


async def test_likely_evidence_is_enough_to_complete() -> None:
    apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    form = _greenhouse_form(apply_url)
    thanks = PageObservation(
        url=apply_url + "/thanks",
        title="Done",
        text="Thank you. Application received.",
    )
    session = FakeSession({apply_url: form, thanks.url: thanks})
    job = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url=apply_url,
        title="Engineer",
        company="Acme",
    )
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="greenhouse")
    outcome = await GreenhouseDriver().run(
        attempt,
        session,
        DriverContext(job=job, profile=CareerProfile(), mode=ExecutionMode.AUTONOMOUS_SUBMIT),
    )
    assert outcome.kind is DriverOutcomeKind.COMPLETED
    assert attempt.evidence.certainty is SubmissionCertainty.LIKELY
    assert any(
        checkpoint.kind == CheckpointKind.SUBMISSION_CONFIRMED.value
        for checkpoint in attempt.checkpoints
    )
    assert session.idempotency_keys == [f"application-attempt:{attempt.id}:submit"]


class _CrashAfterClick(FakeSession):
    async def click(
        self,
        locator: str,
        *,
        label: str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        await super().click(locator, label=label, idempotency_key=idempotency_key)
        msg = "process crashed after submit"
        raise RuntimeError(msg)


async def test_submission_attempted_is_committed_before_the_click(database) -> None:
    apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    form = _greenhouse_form(apply_url)
    job = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url=apply_url,
        title="Engineer",
        company="Acme",
    )
    attempt = ApplicationAttempt(application_id="a", job_id=job.id, driver="greenhouse")

    class _CheckingSession(FakeSession):
        async def click(
            self,
            locator: str,
            *,
            label: str | None = None,
            idempotency_key: str | None = None,
        ) -> ActionResult:
            async with database.session() as other:
                loaded = await AttemptRepository(other).get(attempt.id)
            assert loaded is not None
            assert loaded.submission_attempted_at is not None
            return await super().click(locator, label=label, idempotency_key=idempotency_key)

    session = _CheckingSession({apply_url: form})
    async with database.session() as db_session:
        repos = Repositories.bind(db_session)
        await repos.attempts.save(attempt)

        async def persist(current: ApplicationAttempt) -> None:
            await repos.attempts.save(current)
            await db_session.commit()

        await GreenhouseDriver().run(
            attempt,
            session,
            DriverContext(
                job=job,
                profile=CareerProfile(),
                mode=ExecutionMode.AUTONOMOUS_SUBMIT,
                persist=persist,
            ),
        )
    assert session.clicks == ["submit"]


async def test_crash_after_click_does_not_submit_again_on_restart(database) -> None:
    apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    form = _greenhouse_form(apply_url)
    job = build_job(
        source="greenhouse",
        tier=SourceTier.DIRECT_ATS,
        source_job_id="1",
        url=apply_url,
        title="Engineer",
        company="Acme",
    )
    attempt = ApplicationAttempt(application_id="a", job_id=job.id, driver="greenhouse")
    crashing = _CrashAfterClick({apply_url: form})
    async with database.session() as db_session:
        repos = Repositories.bind(db_session)
        await repos.attempts.save(attempt)

        async def persist(current: ApplicationAttempt) -> None:
            await repos.attempts.save(current)
            await db_session.commit()

        with contextlib.suppress(RuntimeError):
            await GreenhouseDriver().run(
                attempt,
                crashing,
                DriverContext(
                    job=job,
                    profile=CareerProfile(),
                    mode=ExecutionMode.AUTONOMOUS_SUBMIT,
                    persist=persist,
                ),
            )
    assert crashing.clicks == ["submit"]
    async with database.session() as other:
        restarted = await AttemptRepository(other).get(attempt.id)
    assert restarted is not None
    assert restarted.submission_attempted_at is not None
    replay = FakeSession({apply_url: form})
    outcome = await GreenhouseDriver().run(
        restarted,
        replay,
        DriverContext(job=job, profile=CareerProfile(), mode=ExecutionMode.AUTONOMOUS_SUBMIT),
    )
    assert replay.clicks == []
    assert outcome.kind is DriverOutcomeKind.WAITING_FOR_HUMAN
