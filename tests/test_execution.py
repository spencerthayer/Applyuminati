"""HITL, ApplicationAttempt persistence, inbox, and task BLOCKED handling."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast
from unittest.mock import patch

from applyuminati.applications.detect import detect_job
from applyuminati.applications.driver import DriverContext, DriverOutcome, DriverOutcomeKind
from applyuminati.applications.runner import agent_still_owns
from applyuminati.browser.base import BrowserSession, ControlOwner
from applyuminati.browser.host_manager import BrowserHostManager, LiveHost
from applyuminati.browser.host_protocol import (
    BackendAdvertisement,
    CommandMessage,
    HostCommand,
    HostErrorCode,
    RegisterMessage,
    ResultMessage,
)
from applyuminati.core.errors import FailureCategory, NeedsHumanError
from applyuminati.core.logging import get_logger
from applyuminati.core.models.browser_host import BrowserHostRecord
from applyuminati.core.models.execution import (
    ApplicationAttempt,
    AttemptEventKind,
    InterventionReason,
    InterventionResolution,
    WorkflowState,
)
from applyuminati.core.models.job import AtsVendor, Job, SourceTier
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.task import TaskState
from applyuminati.core.settings import ExecutionMode
from applyuminati.db.repositories.attempts import AttemptRepository
from applyuminati.db.repositories.jobs import JobRepository
from applyuminati.db.repositories.tasks import TaskRepository
from applyuminati.plugins.applications.greenhouse import GreenhouseDriver
from applyuminati.services.attempt_service import AttemptService, HostPresence, host_presence
from applyuminati.services.attempt_tasks import (
    APPLICATION_ATTEMPT_KIND,
    ApplicationAttemptPayload,
    run_application_attempt,
    run_attempt_worker_forever,
)
from applyuminati.services.container import Repositories
from applyuminati.services.hosted_session import HostedBrowserSession
from applyuminati.sources.normalize import build_job
from applyuminati.tasks.handlers import TaskContext
from applyuminati.tasks.queue import TaskQueue
from applyuminati.tasks.worker import TaskWorker

HOST_ID = "spencers-mac"


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        return None


async def _attach(manager: BrowserHostManager) -> tuple[LiveHost, FakeConnection]:
    connection = FakeConnection()
    live = await manager.attach(
        BrowserHostRecord(host_id=HOST_ID, credential_hash="x" * 64),
        connection,
        RegisterMessage.model_validate(
            {
                "seq": 1,
                "host_id": HOST_ID,
                "credential": "secret-value",
                "platform": "darwin",
                "backends": {
                    "ego_lite": BackendAdvertisement(available=True, preferred=True),
                },
            }
        ),
    )
    return live, connection


def _job() -> Job:
    return build_job(
        source="linkedin",
        tier=SourceTier.AGGREGATOR,
        source_job_id="99",
        url="https://www.linkedin.com/jobs/view/99",
        title="Staff Engineer",
        company="Acme",
        apply_url="https://boards.greenhouse.io/acme/jobs/99",
    )


async def test_waiting_for_human_is_not_a_failure() -> None:
    attempt = ApplicationAttempt(
        application_id="a",
        job_id="j",
        driver="greenhouse",
    )
    intervention = attempt.open_intervention(
        InterventionReason.AUTHENTICATION_REQUIRED, "Sign into Acme"
    )
    assert attempt.workflow_state is WorkflowState.WAITING_FOR_HUMAN
    assert intervention.open
    assert attempt.workflow_state not in {WorkflowState.FAILED, WorkflowState.CANCELLED}


def test_handoff_override_can_keep_the_agent_in_control() -> None:
    attempt = ApplicationAttempt(application_id="a", job_id="j", driver="greenhouse")
    intervention = attempt.open_intervention(
        InterventionReason.CAPTCHA_REQUIRED,
        "This challenge is actually a form question",
        requires_browser_handoff=False,
    )
    assert intervention.requires_browser_handoff is False


async def test_keep_control_does_not_resume(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1",
            job=job,
            profile=CareerProfile(),
            mode=ExecutionMode.FILL_NO_SUBMIT,
        )
        attempt.open_intervention(InterventionReason.CAPTCHA_REQUIRED, "Complete the challenge")
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        kept = await service.resolve(attempt.id, opened.id, InterventionResolution.KEEP_CONTROL)
        assert kept.workflow_state is WorkflowState.WAITING_FOR_HUMAN
        assert kept.pending_intervention is not None
        assert kept.events[-1].kind is AttemptEventKind.CONTROL_KEPT
        assert not any(
            event.kind is AttemptEventKind.INTERVENTION_RESOLVED for event in kept.events
        )
        queued = await repos.tasks.list()
        assert [task for task in queued if task.kind == APPLICATION_ATTEMPT_KIND] == []


async def test_done_continue_releases_the_pause(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.open_intervention(
            InterventionReason.AMBIGUOUS_QUESTION,
            "Question needs an answer",
            requires_browser_handoff=False,
        )
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        resumed = await service.resolve(attempt.id, opened.id, InterventionResolution.DONE_CONTINUE)
        assert resumed.workflow_state is WorkflowState.PENDING
        assert resumed.pending_intervention is None
        queued = await repos.tasks.list()
        attempt_tasks = [task for task in queued if task.kind == APPLICATION_ATTEMPT_KIND]
        assert len(attempt_tasks) == 1
        assert attempt_tasks[0].payload == {"attempt_id": attempt.id}
        again = await service.resolve(attempt.id, opened.id, InterventionResolution.DONE_CONTINUE)
        assert again.workflow_state is WorkflowState.PENDING
        queued_again = await repos.tasks.list()
        assert len([task for task in queued_again if task.kind == APPLICATION_ATTEMPT_KIND]) == 1


async def test_repeated_resolution_after_task_completion_does_not_enqueue_again(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.open_intervention(
            InterventionReason.AMBIGUOUS_QUESTION,
            "Question needs an answer",
            requires_browser_handoff=False,
        )
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        await service.resolve(attempt.id, opened.id, InterventionResolution.DONE_CONTINUE)
        queue = TaskQueue(repos.tasks)
        claimed = await queue.claim(kinds=[APPLICATION_ATTEMPT_KIND])
        assert claimed is not None
        await queue.complete(claimed, {"status": "waiting_for_human"})
        await service.resolve(attempt.id, opened.id, InterventionResolution.DONE_CONTINUE)
        remaining = [
            task
            for task in await repos.tasks.list()
            if task.kind == APPLICATION_ATTEMPT_KIND and task.state is TaskState.PENDING
        ]
        assert remaining == []


async def _answer_host(
    manager: BrowserHostManager,
    live: Any,
    connection: FakeConnection,
    *,
    reclaim_ok: bool,
    created_sessions: list[CommandMessage] | None = None,
) -> asyncio.Task[None]:
    seen = 0
    owner = "user"

    async def pump() -> None:
        nonlocal seen, owner
        while True:
            await asyncio.sleep(0)
            if len(connection.sent) <= seen:
                continue
            sent = CommandMessage.model_validate_json(connection.sent[seen])
            seen += 1
            if sent.command is HostCommand.CREATE_SESSION:
                if created_sessions is not None:
                    created_sessions.append(sent)
                # A real host answers with the identity it actually opened.
                created_id = f"host-session-{len(created_sessions or [1])}"
                live.open_sessions.add(created_id)
                await manager.handle_result(
                    live,
                    ResultMessage(
                        command_id=sent.id,
                        ok=True,
                        result={
                            "session_id": created_id,
                            "backend": "ego_lite",
                            "task_space_id": sent.params.get("task_space"),
                        },
                    ),
                )
            elif sent.command is HostCommand.RECLAIM_CONTROL:
                ok = reclaim_ok and bool(sent.params.get("confirmed_by_user"))
                if ok:
                    owner = "agent"
                await manager.handle_result(
                    live,
                    ResultMessage(
                        command_id=sent.id,
                        ok=ok,
                        result={"ok": ok, "action": "reclaim"} if ok else {},
                        error_code=None if ok else HostErrorCode.USER_HAS_CONTROL,
                        error_message=None if ok else "user still typing",
                    ),
                )
            elif sent.command is HostCommand.CONTROL_STATE:
                await manager.handle_result(
                    live,
                    ResultMessage(command_id=sent.id, ok=True, result={"owner": owner}),
                )
            else:
                await manager.handle_result(
                    live,
                    ResultMessage(
                        command_id=sent.id,
                        ok=True,
                        result={"ok": True, "action": sent.command.value},
                    ),
                )

    return asyncio.create_task(pump())


async def test_done_continue_reclaims_browser_ownership_before_enqueue(database) -> None:
    job = _job()
    manager = BrowserHostManager()
    live, connection = await _attach(manager)
    live.open_sessions.add("s1")
    pump = await _answer_host(manager, live, connection, reclaim_ok=True)
    try:
        async with database.session() as session:
            await JobRepository(session).upsert(job)
            repos = Repositories.bind(session)
            service = AttemptService(repos)
            attempt = await service.create(
                application_id="app1",
                job=job,
                profile=None,
                mode=ExecutionMode.FILL_NO_SUBMIT,
                browser_host_id=HOST_ID,
            )
            attempt.browser_session_id = "s1"
            attempt.open_intervention(InterventionReason.AUTHENTICATION_REQUIRED, "Sign in")
            await repos.attempts.save(attempt)
            opened = attempt.pending_intervention
            assert opened is not None
            resumed = await service.resolve(
                attempt.id,
                opened.id,
                InterventionResolution.DONE_CONTINUE,
                manager=manager,
            )
            assert resumed.workflow_state is WorkflowState.PENDING
            assert resumed.pending_intervention is None
            queued = [
                task for task in await repos.tasks.list() if task.kind == APPLICATION_ATTEMPT_KIND
            ]
            assert len(queued) == 1
            frames = [CommandMessage.model_validate_json(frame) for frame in connection.sent]
            commands = [frame.command for frame in frames]
            assert HostCommand.RECLAIM_CONTROL in commands
            assert HostCommand.CONTROL_STATE in commands
            assert commands.index(HostCommand.RECLAIM_CONTROL) < commands.index(
                HostCommand.CONTROL_STATE
            )
            reclaim = next(
                frame for frame in frames if frame.command is HostCommand.RECLAIM_CONTROL
            )
            assert reclaim.params.get("confirmed_by_user") is True
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump


async def test_failed_reclaim_keeps_the_intervention_open(database) -> None:
    job = _job()
    manager = BrowserHostManager()
    live, connection = await _attach(manager)
    live.open_sessions.add("s1")
    pump = await _answer_host(manager, live, connection, reclaim_ok=False)
    try:
        async with database.session() as session:
            await JobRepository(session).upsert(job)
            repos = Repositories.bind(session)
            service = AttemptService(repos)
            attempt = await service.create(
                application_id="app1",
                job=job,
                profile=None,
                mode=ExecutionMode.FILL_NO_SUBMIT,
                browser_host_id=HOST_ID,
            )
            attempt.browser_session_id = "s1"
            attempt.open_intervention(InterventionReason.AUTHENTICATION_REQUIRED, "Sign in")
            await repos.attempts.save(attempt)
            opened = attempt.pending_intervention
            assert opened is not None
            kept = await service.resolve(
                attempt.id,
                opened.id,
                InterventionResolution.DONE_CONTINUE,
                manager=manager,
            )
            assert kept.workflow_state is WorkflowState.WAITING_FOR_HUMAN
            assert kept.pending_intervention is not None
            assert kept.events[-1].kind is AttemptEventKind.INTERVENTION_RECLAIM_FAILED
            assert not any(
                event.kind is AttemptEventKind.INTERVENTION_RESOLVED for event in kept.events
            )
            queued = [
                task for task in await repos.tasks.list() if task.kind == APPLICATION_ATTEMPT_KIND
            ]
            assert queued == []
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump


async def test_offline_host_keeps_handoff_paused_until_reconnect(database) -> None:
    job = _job()
    manager = BrowserHostManager()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1",
            job=job,
            profile=None,
            mode=ExecutionMode.FILL_NO_SUBMIT,
            browser_host_id=HOST_ID,
        )
        attempt.browser_session_id = "s1"
        attempt.open_intervention(InterventionReason.AUTHENTICATION_REQUIRED, "Sign in")
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        paused = await service.resolve(
            attempt.id,
            opened.id,
            InterventionResolution.DONE_CONTINUE,
            manager=manager,
        )
        assert paused.workflow_state is WorkflowState.WAITING_FOR_HUMAN
        assert paused.pending_intervention is not None
        assert paused.pending_intervention.id == opened.id
        assert paused.events[-1].kind is AttemptEventKind.INTERVENTION_RECLAIM_FAILED
        assert paused.events[-1].message == "Browser Host unavailable; intervention remains open"
        assert [
            task for task in await repos.tasks.list() if task.kind == APPLICATION_ATTEMPT_KIND
        ] == []

        live, connection = await _attach(manager)
        live.open_sessions.add("s1")
        pump = await _answer_host(manager, live, connection, reclaim_ok=True)
        try:
            resumed = await service.resolve(
                attempt.id,
                opened.id,
                InterventionResolution.DONE_CONTINUE,
                manager=manager,
            )
            assert resumed.workflow_state is WorkflowState.PENDING
            assert resumed.pending_intervention is None
            queued = [
                task for task in await repos.tasks.list() if task.kind == APPLICATION_ATTEMPT_KIND
            ]
            assert len(queued) == 1
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump


async def test_hosted_click_uses_the_caller_idempotency_key() -> None:
    class _Recorder:
        def __init__(self) -> None:
            self.keys: list[str | None] = []

        async def dispatch(self, *_args: Any, idempotency_key: str | None = None, **_kwargs: Any):
            self.keys.append(idempotency_key)
            return ResultMessage(command_id="c1", ok=True, result={"ok": True, "action": "click"})

    recorder = _Recorder()
    hosted = HostedBrowserSession(cast(Any, recorder), HOST_ID, "s1")
    await hosted.click("submit", idempotency_key="application-attempt:att1:submit")
    await hosted.click("submit")
    assert recorder.keys == ["application-attempt:att1:submit", None]


async def test_skip_cancels_the_attempt(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.open_intervention(InterventionReason.AUTHENTICATION_REQUIRED, "Sign in")
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        skipped = await service.resolve(
            attempt.id, opened.id, InterventionResolution.SKIP_APPLICATION
        )
        assert skipped.workflow_state is WorkflowState.CANCELLED


async def test_attempt_round_trips_checkpoints(database) -> None:
    attempt = ApplicationAttempt(
        application_id="app",
        job_id="job",
        driver="greenhouse",
        task_space_id="applyuminati:app",
    )
    attempt.record_checkpoint("application_opened", url="https://example.com")
    async with database.session() as session:
        repo = AttemptRepository(session)
        await repo.save(attempt)
        loaded = await repo.get(attempt.id)
    assert loaded is not None
    assert loaded.task_space_id == "applyuminati:app"
    assert loaded.latest_checkpoint is not None
    assert loaded.latest_checkpoint.kind == "application_opened"


async def test_inbox_lists_only_open_interventions(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.open_intervention(
            InterventionReason.AMBIGUOUS_QUESTION,
            'Question needs an answer: "Are you willing to travel?"',
            requires_browser_handoff=False,
            question_text="Are you willing to travel?",
        )
        await repos.attempts.save(attempt)
        inbox = await service.inbox()
    assert len(inbox) == 1
    assert inbox[0].company == "Acme"
    assert inbox[0].intervention.requires_browser_handoff is False


async def test_needs_human_blocks_instead_of_failing(database) -> None:
    async with database.session() as session:
        queue = TaskQueue(TaskRepository(session))
        await queue.submit("application.execute", {"attempt_id": "x"})
        claimed = await queue.claim()
        assert claimed is not None
        failed = await queue.fail(claimed, NeedsHumanError("sign in"))
        assert failed.state is TaskState.BLOCKED
        assert failed.failure_category is FailureCategory.NEEDS_HUMAN
        requeued = await queue.requeue(failed)
        assert requeued.state is TaskState.PENDING
        assert requeued.attempt_count == failed.attempt_count


def test_greenhouse_url_is_detected_from_a_linkedin_job() -> None:
    job = _job()
    detection = detect_job(job)
    assert detection.ats is AtsVendor.GREENHOUSE


async def test_two_attempts_for_one_application_get_distinct_task_spaces(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        first = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        second = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
    assert first.task_space_id == f"applyuminati:{first.id}"
    assert second.task_space_id == f"applyuminati:{second.id}"
    assert first.task_space_id != second.task_space_id


class _Conn:
    async def send_text(self, payload: str) -> None:
        return None

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        return None


class _PausedDriver:
    metadata = GreenhouseDriver().metadata

    def detects(self, url: str):
        return GreenhouseDriver().detects(url)

    async def run(
        self,
        attempt: ApplicationAttempt,
        session: BrowserSession,
        context: DriverContext,
    ) -> DriverOutcome:
        attempt.open_intervention(InterventionReason.CAPTCHA_REQUIRED, "Solve the challenge")
        return DriverOutcome(kind=DriverOutcomeKind.WAITING_FOR_HUMAN, attempt=attempt)


async def _session_factory(_attempt: ApplicationAttempt) -> BrowserSession:
    return cast(BrowserSession, object())


async def test_attempt_handler_releases_the_queue_on_human_pause(database) -> None:
    job = _job()

    async def _noop_checkpoint(state: dict[str, Any]) -> None:
        return None

    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1", job=job, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.open_intervention(
            InterventionReason.AMBIGUOUS_QUESTION,
            "Question needs an answer",
            requires_browser_handoff=False,
        )
        await repos.attempts.save(attempt)
        opened = attempt.pending_intervention
        assert opened is not None
        await service.resolve(attempt.id, opened.id, InterventionResolution.DONE_CONTINUE)
        queue = TaskQueue(repos.tasks)
        claimed = await queue.claim(kinds=[APPLICATION_ATTEMPT_KIND])
        assert claimed is not None
        prior_attempts = claimed.attempt_count
        result = await run_application_attempt(
            ApplicationAttemptPayload(attempt_id=attempt.id),
            TaskContext(
                task_id=claimed.id,
                kind=APPLICATION_ATTEMPT_KIND,
                run_id=None,
                attempt=1,
                strategy=None,
                resume_state={},
                logger=get_logger(__name__),
                checkpoint_sink=_noop_checkpoint,
            ),
            repos=repos,
            driver=_PausedDriver(),
            session_factory=_session_factory,
        )
        assert result["status"] == WorkflowState.WAITING_FOR_HUMAN.value
        loaded = await repos.attempts.get(attempt.id)
        assert loaded is not None
        assert loaded.workflow_state is WorkflowState.WAITING_FOR_HUMAN
        finished = await queue.complete(claimed, result)
        assert finished.state is TaskState.SUCCEEDED
        assert finished.attempt_count == prior_attempts


async def test_inbox_reports_live_host_presence(database) -> None:
    job = _job()
    async with database.session() as session:
        await JobRepository(session).upsert(job)
        repos = Repositories.bind(session)
        service = AttemptService(repos)
        attempt = await service.create(
            application_id="app1",
            job=job,
            profile=None,
            mode=ExecutionMode.FILL_NO_SUBMIT,
            browser_host_id="mac",
        )
        intervention = attempt.open_intervention(
            InterventionReason.AUTHENTICATION_REQUIRED, "Sign in"
        )
        await repos.attempts.save(attempt)
        manager = BrowserHostManager()
        assert host_presence(attempt, intervention, manager).value == "offline"
        live_record = BrowserHostRecord(host_id="mac")
        manager._hosts["mac"] = LiveHost(record=live_record, connection=_Conn())
        assert host_presence(attempt, intervention, manager).value == "connected"


async def test_reclaim_targets_the_host_the_intervention_recorded(database) -> None:
    job = _job()
    manager = BrowserHostManager()
    live, connection = await _attach(manager)
    live.open_sessions.add("s1")
    pump = await _answer_host(manager, live, connection, reclaim_ok=True)
    try:
        async with database.session() as session:
            await JobRepository(session).upsert(job)
            repos = Repositories.bind(session)
            service = AttemptService(repos)
            attempt = await service.create(
                application_id="app1",
                job=job,
                profile=None,
                mode=ExecutionMode.FILL_NO_SUBMIT,
                browser_host_id="retired-mac",
            )
            opened = attempt.open_intervention(
                InterventionReason.AUTHENTICATION_REQUIRED, "Sign in"
            )
            opened.browser_host_id = HOST_ID
            opened.browser_session_id = "s1"
            await repos.attempts.save(attempt)
            assert host_presence(attempt, opened, manager) is HostPresence.CONNECTED
            resumed = await service.resolve(
                attempt.id,
                opened.id,
                InterventionResolution.DONE_CONTINUE,
                manager=manager,
            )
            assert resumed.workflow_state is WorkflowState.PENDING
            assert resumed.browser_host_id == HOST_ID
            assert resumed.browser_session_id == "s1"
            commands = [
                CommandMessage.model_validate_json(frame).command for frame in connection.sent
            ]
            assert HostCommand.RECLAIM_CONTROL in commands
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump


async def test_open_browser_persists_the_session_it_hands_to_the_human(database) -> None:
    job = _job()
    manager = BrowserHostManager()
    live, connection = await _attach(manager)
    created: list[CommandMessage] = []
    pump = await _answer_host(manager, live, connection, reclaim_ok=True, created_sessions=created)
    try:
        async with database.session() as session:
            await JobRepository(session).upsert(job)
            repos = Repositories.bind(session)
            service = AttemptService(repos)
            attempt = await service.create(
                application_id="app1",
                job=job,
                profile=None,
                mode=ExecutionMode.FILL_NO_SUBMIT,
                browser_host_id=HOST_ID,
            )
            opened = attempt.open_intervention(
                InterventionReason.AUTHENTICATION_REQUIRED, "Sign in"
            )
            await repos.attempts.save(attempt)
            assert opened.browser_session_id is None

            handed = await service.activate_browser(attempt, manager=manager, instruction="Sign in")
            assert handed["ok"] is True
            assert len(created) == 1
            # Durable execution identity, not a host-invented name.
            assert created[0].params["task_space"] == f"applyuminati:{attempt.id}"
            assert created[0].params["session_id"] == attempt.id

        async with database.session() as fresh:
            reloaded = await AttemptRepository(fresh).get(attempt.id)
        assert reloaded is not None
        assert reloaded.browser_session_id == "host-session-1"
        assert reloaded.task_space_id == f"applyuminati:{attempt.id}"
        pending = reloaded.pending_intervention
        assert pending is not None
        assert pending.browser_session_id == "host-session-1"
        assert pending.task_space_id == reloaded.task_space_id

        async with database.session() as session:
            repos = Repositories.bind(session)
            service = AttemptService(repos)
            resumed = await service.resolve(
                reloaded.id,
                pending.id,
                InterventionResolution.DONE_CONTINUE,
                manager=manager,
            )
            assert resumed.workflow_state is WorkflowState.PENDING
            assert resumed.browser_session_id == "host-session-1"
        # Reclaim reused the handed-off session instead of creating another.
        assert len(created) == 1
        reclaims = [
            CommandMessage.model_validate_json(frame)
            for frame in connection.sent
            if CommandMessage.model_validate_json(frame).command is HostCommand.RECLAIM_CONTROL
        ]
        assert [frame.session_id for frame in reclaims] == ["host-session-1"]
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump


async def test_control_state_fails_closed_when_the_host_errors() -> None:
    class _Broken:
        async def dispatch(self, *_args: Any, **_kwargs: Any) -> ResultMessage:
            return ResultMessage(
                command_id="c1",
                ok=False,
                error_code=HostErrorCode.BACKEND_UNAVAILABLE,
                error_message="host went away",
            )

    hosted = HostedBrowserSession(cast(Any, _Broken()), HOST_ID, "s1")
    assert await hosted.control_state() is ControlOwner.USER
    assert await agent_still_owns(cast(Any, hosted)) is False


async def test_wait_for_control_polls_until_the_user_returns() -> None:
    class _Owners:
        def __init__(self) -> None:
            self.calls = 0

        async def dispatch(self, *_args: Any, **_kwargs: Any) -> ResultMessage:
            self.calls += 1
            owner = "user" if self.calls < 3 else "agent"
            return ResultMessage(command_id="c1", ok=True, result={"owner": owner})

    owners = _Owners()
    hosted = HostedBrowserSession(cast(Any, owners), HOST_ID, "s1")
    with patch("applyuminati.services.hosted_session._CONTROL_POLL_SECONDS", 0.0):
        granted = await hosted.wait_for_control(timeout_seconds=5.0)
    assert granted.ok is True
    assert owners.calls == 3


async def test_wait_for_control_reports_a_timeout_without_seizing() -> None:
    class _StillTheirs:
        def __init__(self) -> None:
            self.calls = 0

        async def dispatch(self, *_args: Any, **_kwargs: Any) -> ResultMessage:
            self.calls += 1
            return ResultMessage(command_id="c1", ok=True, result={"owner": "user"})

    theirs = _StillTheirs()
    hosted = HostedBrowserSession(cast(Any, theirs), HOST_ID, "s1")
    with patch("applyuminati.services.hosted_session._CONTROL_POLL_SECONDS", 0.0):
        refused = await hosted.wait_for_control(timeout_seconds=0.05)
    assert refused.ok is False
    assert theirs.calls >= 1
    assert hosted.owner is not ControlOwner.AGENT


async def test_worker_survives_a_crashing_poll(database, monkeypatch) -> None:
    stop = asyncio.Event()
    polls = 0

    async def _run_once(self: Any, **_kwargs: Any) -> bool:
        nonlocal polls
        polls += 1
        if polls == 1:
            msg = "database went away"
            raise RuntimeError(msg)
        stop.set()
        return False

    monkeypatch.setattr(TaskWorker, "run_once", _run_once)
    await asyncio.wait_for(
        run_attempt_worker_forever(poll_interval=0.01, stop_event=stop),
        timeout=5.0,
    )
    assert polls >= 2
