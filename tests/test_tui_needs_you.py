"""The Needs You screen: resolving human handoffs from the terminal.

Autonomous submission is only safe to ship if a human can see every pause and
close it, so these tests drive the screen through the real ``AttemptService``
and assert on what the widgets say and what the database holds afterwards.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("textual", reason="the tui extra is not installed")

from textual.app import App
from textual.widgets import DataTable, Input, Static

from applyuminati.core.models.execution import (
    AttemptEventKind,
    InterventionReason,
    InterventionResolution,
    WorkflowState,
)
from applyuminati.core.models.job import SourceTier
from applyuminati.core.models.jsonresume import JsonResume, ResumeBasics
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.models.questionnaire import AnswerDraft, AnswerStatus
from applyuminati.core.settings import ExecutionMode
from applyuminati.services.attempt_service import (
    APPLICATION_ATTEMPT_KIND,
    AttemptService,
)
from applyuminati.sources.normalize import build_job

_INSTRUCTION = "Sign in to Acme and finish the captcha"
_QUESTION = "How much notice period do you need?"
_QUESTION_KEY = "how much notice period do you need"


def _job():
    return build_job(
        source="linkedin",
        tier=SourceTier.AGGREGATOR,
        source_job_id="77",
        url="https://www.linkedin.com/jobs/view/77",
        title="Staff Engineer",
        company="Acme",
        apply_url="https://boards.greenhouse.io/acme/jobs/77",
    )


async def _seed_waiting(
    container,
    *,
    reason: InterventionReason = InterventionReason.CAPTCHA_REQUIRED,
    handoff: bool = True,
    question: bool = False,
):
    """An attempt parked in WAITING_FOR_HUMAN with one open intervention.

    Built through ``start_for_job`` so the pause is the shape a real application
    has, then forced into the waiting state with an open intervention.
    """
    job = _job()
    async with container.repositories() as repos:
        await repos.jobs.upsert(job)
        await repos.profiles.upsert(
            CareerProfile(id="p1", label="t", resume=JsonResume(basics=ResumeBasics(name="T")))
        )
        service = AttemptService(repos)
        attempt = await service.start_for_job(
            job_id=job.id, profile=None, mode=ExecutionMode.FILL_NO_SUBMIT
        )
        attempt.browser_host_id = "host-1"
        attempt.open_intervention(
            reason,
            _INSTRUCTION,
            requires_browser_handoff=handoff,
            question_key=_QUESTION_KEY if question else None,
            question_text=_QUESTION if question else None,
        )
        if question:
            attempt.answers.append(AnswerDraft(question_key=_QUESTION_KEY, question_text=_QUESTION))
        await repos.attempts.save(attempt)
    return attempt


class _ScreenOnlyApp(App[None]):
    """The screen's whole dependency is ``app.container``.

    ``TuiApp`` also starts the attempt worker, which claims a pending
    application.attempt task and drives a real browser against it. That is
    correct for the real app, but it would race these tests for the very task
    they are asserting about, so the screen is mounted on an app that supplies
    the container and nothing else. The one test that needs the real app is
    :func:`test_the_screen_is_reachable_from_the_real_app`.
    """

    def __init__(self, container) -> None:
        super().__init__()
        self.container = container


@contextlib.asynccontextmanager
async def _screen(container, keys: tuple[str, ...] = ()) -> AsyncIterator[App[None]]:
    """Mount ``NeedsYouScreen``, press keys, yield the running app.

    Every key is pressed after the previous one has fully settled, so a worker
    still writing when the next key arrives cannot be mistaken for its result.
    """
    from applyuminati.tui.screens.needs_you import NeedsYouScreen

    app = _ScreenOnlyApp(container)
    async with app.run_test() as pilot:
        await app.push_screen(NeedsYouScreen())
        await _settle(pilot)
        for key in keys:
            await pilot.press(key)
            await _settle(pilot)
        yield app


async def _settle(pilot) -> None:
    """Let every worker the screen started run to completion."""
    for _ in range(8):
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()


def _rows(app: App[None]) -> list[list[str]]:
    table = app.screen.query_one("#inbox-table", DataTable)
    return [list(table.get_row_at(index)) for index in range(table.row_count)]


def _text(app: App[None], selector: str) -> str:
    return str(app.screen.query_one(selector, Static).render())


async def _stored(container, attempt_id: str):
    async with container.read_repositories() as repos:
        return await AttemptService(repos).get(attempt_id)


async def _queued(container):
    async with container.read_repositories() as repos:
        return [task for task in await repos.tasks.list() if task.kind == APPLICATION_ATTEMPT_KIND]


async def test_the_list_shows_the_reason_instruction_and_opened_time(container) -> None:
    """A pause is only actionable if the user can see why it happened and when."""
    await _seed_waiting(container)
    async with _screen(container) as app:
        rows = _rows(app)
        assert len(rows) == 1
        rendered = " | ".join(rows[0])
        assert "captcha_required" in rendered
        assert _INSTRUCTION in rendered
        # Opened at a wall-clock minute, not just a count of items.
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", rows[0][3]), rows[0][3]
        # A populated inbox must not also claim nothing needs you.
        assert app.screen.query_one("#empty-state").display is False


async def test_host_presence_is_surfaced(container) -> None:
    """No Browser Host is connected, so the row must say so.

    This is what stops "resolve" failing confusingly: the user sees the host is
    not reachable before pressing a key that depends on it.
    """
    await _seed_waiting(container)
    async with _screen(container) as app:
        assert "offline" in " ".join(_rows(app)[0]).lower()


async def test_an_in_app_pause_reports_no_host_needed(container) -> None:
    """A pause with no handoff must not claim a Mac is missing."""
    await _seed_waiting(container, reason=InterventionReason.AMBIGUOUS_QUESTION, handoff=False)
    async with _screen(container) as app:
        assert "in-app" in " ".join(_rows(app)[0]).lower()


async def test_done_continue_closes_the_intervention_and_queues_a_resume(container) -> None:
    """`d` is the ordinary path: the human is finished, so the engine continues."""
    attempt = await _seed_waiting(container, handoff=False)
    async with _screen(container, ("d",)):
        pass
    stored = await _stored(container, attempt.id)
    assert stored.pending_intervention is None
    assert stored.workflow_state is WorkflowState.PENDING
    assert stored.interventions[0].resolution is InterventionResolution.DONE_CONTINUE
    assert [task.payload for task in await _queued(container)] == [{"attempt_id": attempt.id}]


async def test_keep_control_leaves_the_attempt_waiting_and_queues_no_resume(container) -> None:
    """The whole point of keep_control: the human still has the browser."""
    attempt = await _seed_waiting(container)
    async with _screen(container, ("k",)) as app:
        rows = _rows(app)
    stored = await _stored(container, attempt.id)
    assert stored.pending_intervention is not None, "keep_control must not close the pause"
    assert stored.workflow_state is WorkflowState.WAITING_FOR_HUMAN
    assert stored.events[-1].kind is AttemptEventKind.CONTROL_KEPT
    assert stored.interventions[0].resolution is None
    # The seed queued one task when the attempt started; keep_control must not
    # add a resume on top of it, and the item must still be listed.
    assert len(await _queued(container)) == 1
    assert len(rows) == 1


async def test_skip_application_cancels_without_resuming(container) -> None:
    """`s` abandons the attempt, and a cancelled attempt must not be picked up."""
    attempt = await _seed_waiting(container)
    async with _screen(container, ("s",)):
        pass
    stored = await _stored(container, attempt.id)
    assert stored.workflow_state is WorkflowState.CANCELLED
    assert stored.pending_intervention is None
    assert stored.interventions[0].resolution is InterventionResolution.SKIP_APPLICATION


async def test_answer_records_the_draft_and_resumes(container) -> None:
    """`a` focuses the answer box; submitting it is the answer resolution."""
    attempt = await _seed_waiting(
        container, reason=InterventionReason.AMBIGUOUS_QUESTION, handoff=False, question=True
    )
    from applyuminati.tui.screens.needs_you import NeedsYouScreen

    app = _ScreenOnlyApp(container)
    async with app.run_test() as pilot:
        await app.push_screen(NeedsYouScreen())
        await _settle(pilot)
        # The answer box names the question it is answering.
        assert app.screen.query_one("#answer-input", Input).placeholder == _QUESTION
        await pilot.press("a")
        await _settle(pilot)
        assert app.screen.query_one("#answer-input", Input).has_focus, (
            "a must focus the answer input"
        )
        await pilot.press(*"4 weeks")
        await pilot.press("enter")
        await _settle(pilot)

    stored = await _stored(container, attempt.id)
    assert stored.pending_intervention is None
    assert stored.workflow_state is WorkflowState.PENDING
    assert stored.interventions[0].resolution is InterventionResolution.ANSWER
    assert stored.answers[0].answer == "4 weeks"
    assert stored.answers[0].status is AnswerStatus.USER_PROVIDED
    assert [task.payload for task in await _queued(container)] == [{"attempt_id": attempt.id}]


async def test_the_list_is_empty_with_an_honest_empty_state(container) -> None:
    """Nothing waiting must read as 'nothing waiting', not as a failed load."""
    async with _screen(container) as app:
        assert _rows(app) == []
        assert app.screen.query_one("#empty-state").display is True
        assert "nothing needs you" in _text(app, "#empty-state").lower()


async def test_open_browser_reports_the_offline_host(container) -> None:
    """`o` asks the host to surface the session and says why it could not."""
    attempt = await _seed_waiting(container)
    async with _screen(container, ("o",)) as app:
        status = _text(app, "#status")
    assert "offline" in status.lower(), status
    # Asking for the browser is not a resolution: the pause stays open.
    stored = await _stored(container, attempt.id)
    assert stored.pending_intervention is not None
    assert stored.workflow_state is WorkflowState.WAITING_FOR_HUMAN


async def test_the_screen_is_reachable_from_the_real_app(container) -> None:
    """The `n` binding's target must resolve and mount on TuiApp.

    Navigated with the app's own ``action_go`` rather than a bare push, so this
    also proves the screen is wired into the app's screen map. Only the empty
    inbox is exercised: TuiApp starts the attempt worker, which would claim a
    seeded task and drive a real browser, and that is not what this is about.
    """
    from applyuminati.tui.app import TuiApp
    from applyuminati.tui.screens.needs_you import NeedsYouScreen

    app = TuiApp(container)
    async with app.run_test() as pilot:
        await _settle(pilot)
        await app.action_go("needs_you")
        await _settle(pilot)
        assert isinstance(app.screen, NeedsYouScreen)
        assert _rows(app) == []
        assert "nothing needs you" in _text(app, "#empty-state").lower()
