"""The shipped default is autonomous submission, and the guard rails are not
governed by that setting.

This test records a deliberate, user-directed reversal of the project's
previous posture. It is here so the change is visible rather than incidental,
and so anyone who disagrees can see exactly one line to change.
"""

from __future__ import annotations

from pathlib import Path

from applyuminati.core.models.jsonresume import (
    JsonResume,
    ResumeBasics,
    ResumeWork,
)
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.settings import ExecutionMode, Settings
from applyuminati.resume.guard import FabricationGuard, GuardSeverity


def test_the_default_execution_mode_is_autonomous_submit(tmp_path: Path) -> None:
    assert Settings(data_dir=tmp_path).execution_mode is ExecutionMode.AUTONOMOUS_SUBMIT


def test_a_user_can_still_choose_a_tighter_mode(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, execution_mode=ExecutionMode.RESEARCH_ONLY)
    assert settings.execution_mode is ExecutionMode.RESEARCH_ONLY


def test_autonomous_mode_still_flags_fabricated_content(tmp_path: Path) -> None:
    """The guard rail that matters most, and it does not consult the mode.

    A mode change must never be able to switch the fabrication guard off, so
    this asserts the guard still fires while the mode is autonomous.
    """
    settings = Settings(data_dir=tmp_path)
    assert settings.execution_mode is ExecutionMode.AUTONOMOUS_SUBMIT

    profile = CareerProfile(
        id="p1",
        label="t",
        resume=JsonResume(
            basics=ResumeBasics(name="Test"),
            work=[ResumeWork(name="Acme", position="Engineer")],
        ),
    )
    fabricated = JsonResume(
        basics=ResumeBasics(name="Test"),
        work=[
            ResumeWork(
                name="Google",
                position="Director",
                highlights=["Reduced costs by 40%"],
            )
        ],
    )
    report = FabricationGuard(profile).check(fabricated)
    assert any(v.severity is GuardSeverity.HARD for v in report.violations), [
        (v.kind, v.path) for v in report.violations
    ]
