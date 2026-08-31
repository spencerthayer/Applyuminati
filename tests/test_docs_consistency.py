"""Facts the docs must not drift from the code."""

from __future__ import annotations

from applyuminati.core.models.application import ApplicationState, TRANSITIONS
from applyuminati.core.models.execution import WorkflowState
from applyuminati.core.registry import PluginMaturity
from applyuminati.services.capabilities import collect_capability_matrix


def test_application_state_has_exactly_twenty_members() -> None:
    assert len(ApplicationState) == 20
    assert set(TRANSITIONS) == set(ApplicationState)


def test_workflow_state_is_a_separate_machine() -> None:
    assert {state.value for state in WorkflowState} == {
        "pending",
        "running",
        "waiting_for_human",
        "waiting_for_provider",
        "retry_scheduled",
        "completed",
        "failed",
        "cancelled",
    }
    assert "waiting_for_human" not in {state.value for state in ApplicationState}


def test_capability_matrix_matches_registries_and_claims_no_production() -> None:
    rows = collect_capability_matrix()
    keys = {(row.kind, row.slug) for row in rows}
    assert ("browser", "ego_lite") in keys
    assert ("browser", "playwright") in keys
    assert ("source", "greenhouse") in keys
    assert ("source", "lever") in keys
    assert ("application_driver", "greenhouse") in keys
    assert ("application_driver", "lever") in keys
    assert all(row.maturity is not PluginMaturity.PRODUCTION_TESTED for row in rows)
    ego = next(row for row in rows if row.kind == "browser" and row.slug == "ego_lite")
    assert ego.maturity is PluginMaturity.WORKFLOW_INTEGRATED
