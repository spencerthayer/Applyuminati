"""ego lite adapter: task-space identity and ownership.

The two bugs covered here were both silent. One meant a first attempt could
never open its browser at all; the other meant the agent seized the browser
back from a person who might have been halfway through typing a password. No
helper is invoked: the generated programs and the ownership state machine are
what is under test, and they are exactly what a macOS-only integration cannot
otherwise exercise in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from applyuminati.browser.base import BrowserCheckpoint, ControlOwner
from applyuminati.core.settings import Settings
from applyuminati.plugins.browsers.ego_lite import (
    RESULT_KEY,
    TASK_SPACE_PREFIX,
    EgoLiteBackend,
    EgoLiteSession,
    EgoSurface,
    HelperRun,
    TaskSpaceRef,
    build_script,
    parse_envelope,
)
from applyuminati.plugins.browsers.ego_lite import (
    _requested_task_space as requested_task_space,
)
from applyuminati.plugins.browsers.ego_lite import (
    _resume_task_space as resume_task_space,
)

SESSION_ID = "01JABCDEFGHJKMNPQRSTVWXYZ"


class FakeBackend:
    """Records scripts and replays canned envelopes instead of spawning a helper."""

    def __init__(self, settings: Settings, replies: list[dict] | None = None) -> None:
        self._settings = settings
        self.scripts: list[str] = []
        self._replies = list(replies or [])

    @property
    def navigation_timeout(self) -> float:
        return 5.0

    @property
    def artifacts_dir(self) -> Path:
        return self._settings.artifacts_dir

    async def run_script(self, script: str, *, timeout: float | None = None) -> HelperRun:
        self.scripts.append(script)
        reply = self._replies.pop(0) if self._replies else {"ok": True, "value": None}
        payload = {RESULT_KEY: True, **reply}
        return HelperRun(returncode=0, stdout=json.dumps(payload), stderr="", duration_ms=1.0)


def _session(
    settings: Settings,
    *,
    replies: list[dict] | None = None,
    task_space: TaskSpaceRef | None = None,
    owner: ControlOwner = ControlOwner.AGENT,
) -> tuple[EgoLiteSession, FakeBackend]:
    backend = FakeBackend(settings, replies)
    session = EgoLiteSession(
        backend,  # type: ignore[arg-type]
        session_id=SESSION_ID,
        task_space=task_space or TaskSpaceRef.for_session(SESSION_ID),
        surface=EgoSurface.FLAT,
    )
    session._owner = owner
    return session, backend


# ---------------------------------------------------------------------------
# Task-space identity
# ---------------------------------------------------------------------------


def test_a_new_task_space_is_opened_by_name_not_by_a_derived_number() -> None:
    """The original bug. A number only ever matches an existing space.

    ``useOrCreateTaskSpace`` creates when given a name and looks up when given a
    number, so passing a locally derived number could only work for a space that
    already happened to exist. First use could never create one.
    """
    ref = TaskSpaceRef.for_session(SESSION_ID)
    script = build_script("return 1;", task_space=ref, surface=EgoSurface.FLAT)
    assert f'useOrCreateTaskSpace("{TASK_SPACE_PREFIX}:{SESSION_ID}")' in script


def test_a_known_numeric_id_is_used_once_it_has_been_learned() -> None:
    ref = TaskSpaceRef.for_session(SESSION_ID).with_numeric_id(4812)
    script = build_script("return 1;", task_space=ref, surface=EgoSurface.FLAT)
    assert "useOrCreateTaskSpace(4812)" in script
    assert f"{TASK_SPACE_PREFIX}:{SESSION_ID}" not in script


def test_the_name_stays_stable_for_one_session() -> None:
    assert TaskSpaceRef.for_session(SESSION_ID) == TaskSpaceRef.for_session(SESSION_ID)
    assert TaskSpaceRef.for_session("other").name != TaskSpaceRef.for_session(SESSION_ID).name


async def test_the_numeric_id_reported_by_the_helper_is_remembered(settings) -> None:
    session, _ = _session(settings, replies=[{"ok": True, "value": None, "task_space_id": 991}])
    await session.observe(include_text=False)
    assert session.task_space.numeric_id == 991


async def test_a_missing_numeric_id_is_not_an_error(settings) -> None:
    """Only the program that creates the space learns the id; the rest may not."""
    session, _ = _session(settings, replies=[{"ok": True, "value": None}])
    await session.observe(include_text=False)
    assert session.task_space.numeric_id is None
    assert session.task_space.name.endswith(SESSION_ID)


async def test_a_nonsense_reported_id_is_ignored(settings) -> None:
    session, _ = _session(
        settings, replies=[{"ok": True, "value": None, "task_space_id": "not-a-number"}]
    )
    await session.observe(include_text=False)
    assert session.task_space.numeric_id is None


async def test_the_checkpoint_carries_both_forms_of_identity(settings) -> None:
    session, _ = _session(settings, replies=[{"ok": True, "value": None, "task_space_id": 77}])
    await session.observe(include_text=False)
    checkpoint = await session.checkpoint()
    assert checkpoint.backend_state["task_space_name"].endswith(SESSION_ID)
    assert checkpoint.backend_state["task_space_id"] == 77


def test_resuming_prefers_the_recorded_name() -> None:
    checkpoint = BrowserCheckpoint(
        session_id=SESSION_ID,
        url="https://example.test",
        backend_state={"task_space_name": "applyuminati:earlier", "task_space_id": 12},
    )
    ref = resume_task_space("ignored", checkpoint)
    assert ref.name == "applyuminati:earlier"
    assert ref.numeric_id == 12


def test_resuming_a_checkpoint_without_a_name_falls_back_to_the_session() -> None:
    """Checkpoints written before task spaces were named still resume."""
    checkpoint = BrowserCheckpoint(
        session_id=SESSION_ID, url="", backend_state={"task_space_id": 5}
    )
    ref = resume_task_space(SESSION_ID, checkpoint)
    assert ref.name == f"{TASK_SPACE_PREFIX}:{SESSION_ID}"
    assert ref.numeric_id == 5


def test_resuming_without_a_checkpoint_opens_a_fresh_space() -> None:
    ref = resume_task_space(SESSION_ID, None)
    assert ref == TaskSpaceRef.for_session(SESSION_ID)


def test_a_caller_supplied_task_space_wins_over_the_session_derived_name() -> None:
    """The attempt owns the durable name; the session id is incidental.

    Deriving the name from a local session id records a workspace the resume
    path cannot find again.
    """
    ref = requested_task_space(SESSION_ID, None, "applyuminati:att-42")
    assert ref.name == "applyuminati:att-42"
    assert ref.numeric_id is None


def test_a_requested_name_discards_a_numeric_id_for_a_different_space() -> None:
    checkpoint = BrowserCheckpoint(
        session_id=SESSION_ID,
        url="",
        backend_state={"task_space_name": "applyuminati:earlier", "task_space_id": 12},
    )
    ref = requested_task_space(SESSION_ID, checkpoint, "applyuminati:att-42")
    assert ref.name == "applyuminati:att-42"
    assert ref.numeric_id is None
    same = requested_task_space(SESSION_ID, checkpoint, "applyuminati:earlier")
    assert same.numeric_id == 12


def test_no_requested_name_still_resumes_the_recorded_space() -> None:
    checkpoint = BrowserCheckpoint(
        session_id=SESSION_ID,
        url="",
        backend_state={"task_space_name": "applyuminati:earlier", "task_space_id": 12},
    )
    assert requested_task_space(SESSION_ID, checkpoint, None).name == "applyuminati:earlier"


def test_a_session_reports_the_task_space_it_is_driving(settings: Settings) -> None:
    session, _ = _session(settings, task_space=TaskSpaceRef(name="applyuminati:att-42"))
    assert session.task_space_id == "applyuminati:att-42"


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_waiting_for_control_does_not_take_it() -> None:
    """The second bug. takeOver performs no ownership check.

    The old program polled and then called takeOverTaskSpace in the same script,
    so a poll that timed out still ended with the agent seizing the browser from
    whoever was using it.
    """
    body = build_script(
        "return 1;", task_space=TaskSpaceRef.for_session(SESSION_ID), surface=EgoSurface.FLAT
    )
    assert "takeOverTaskSpace" not in body


async def test_wait_for_control_is_read_only(settings) -> None:
    session, backend = _session(
        settings,
        replies=[{"ok": True, "value": {"granted": True}}],
        owner=ControlOwner.DELEGATED_TO_USER,
    )
    result = await session.wait_for_control(timeout_seconds=1.0)
    assert result.ok
    assert session.owner is ControlOwner.AGENT
    assert "takeOverTaskSpace" not in "".join(backend.scripts)


async def test_a_timeout_leaves_the_session_with_the_user(settings) -> None:
    """A timer is not permission. The person may still be signing in."""
    session, backend = _session(
        settings,
        replies=[{"ok": True, "value": {"granted": False}}],
        owner=ControlOwner.DELEGATED_TO_USER,
    )
    result = await session.wait_for_control(timeout_seconds=1.0)
    assert not result.ok
    assert session.owner is ControlOwner.DELEGATED_TO_USER
    assert "takeOverTaskSpace" not in "".join(backend.scripts)


async def test_reclaim_refuses_without_an_explicit_confirmation(settings) -> None:
    session, backend = _session(settings, owner=ControlOwner.DELEGATED_TO_USER)
    result = await session.reclaim_control(confirmed_by_user=False)
    assert not result.ok
    assert "explicit user confirmation" in (result.detail or "")
    assert session.owner is ControlOwner.DELEGATED_TO_USER
    assert backend.scripts == []


async def test_reclaim_takes_over_when_the_user_confirmed(settings) -> None:
    session, backend = _session(
        settings,
        replies=[{"ok": True, "value": {"reclaimed": True}}],
        owner=ControlOwner.DELEGATED_TO_USER,
    )
    result = await session.reclaim_control(confirmed_by_user=True)
    assert result.ok
    assert session.owner is ControlOwner.AGENT
    assert "takeOverTaskSpace" in "".join(backend.scripts)


async def test_the_agent_refuses_to_act_while_the_user_holds_the_session(settings) -> None:
    session, backend = _session(settings, owner=ControlOwner.DELEGATED_TO_USER)
    result = await session.fill_field("#name", "Jane")
    assert not result.ok
    assert "wait_for_control" in (result.detail or "")
    assert backend.scripts == []


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("agent", ControlOwner.AGENT),
        ("agentDelegatedToUser", ControlOwner.DELEGATED_TO_USER),
        ("user", ControlOwner.USER),
    ],
)
async def test_control_state_adopts_ego_ownership(settings, reported, expected) -> None:
    """We map onto ego lite's model rather than running a competing one."""
    session, _ = _session(settings, replies=[{"ok": True, "value": {"ownership": reported}}])
    assert await session.control_state() is expected


async def test_unknown_ownership_keeps_the_last_known_value(settings) -> None:
    """An older build that cannot answer must not be read as "you may drive"."""
    session, _ = _session(
        settings,
        replies=[{"ok": True, "value": {"ownership": None}}],
        owner=ControlOwner.DELEGATED_TO_USER,
    )
    assert await session.control_state() is ControlOwner.DELEGATED_TO_USER


async def test_closing_does_not_evict_a_user_who_still_holds_the_space(settings) -> None:
    session, backend = _session(settings, owner=ControlOwner.DELEGATED_TO_USER)
    await session.close()
    assert backend.scripts == []


# ---------------------------------------------------------------------------
# Result protocol
# ---------------------------------------------------------------------------


def test_a_thrown_program_still_reports_the_task_space_id() -> None:
    """Exit 1 discards stdout, so the failure path has to emit, not rethrow."""
    script = build_script(
        "throw new Error('x');",
        task_space=TaskSpaceRef.for_session(SESSION_ID),
        surface=EgoSurface.FLAT,
    )
    assert script.count("task_space_id:") == 2
    assert "ok: false" in script


def test_the_envelope_is_found_among_other_output() -> None:
    stdout = 'noise\n{"__applyuminati__": true, "ok": true, "value": 3}\nmore noise'
    envelope = parse_envelope(stdout)
    assert envelope is not None
    assert envelope["value"] == 3


async def test_a_backend_without_the_helper_is_not_selectable(settings) -> None:
    """CI runs on Linux, where health must say so rather than try to run it."""
    report = await EgoLiteBackend(settings).health()
    assert not report.usable
    assert "macOS" in report.detail
