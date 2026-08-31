"""CI/release gating is an invariant, so it is tested like one.

The failure this guards against is silent and expensive: a release workflow
with its own `push` trigger publishes `latest` in parallel with CI, so a commit
that fails validation still ships an image and every deployment tracking
`latest` picks it up. Reviewing YAML by eye does not catch a reintroduced
trigger; these assertions do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

#: Every job that must succeed before an image may be published.
REQUIRED_VALIDATION_JOBS = {"python", "web", "docker"}


def _load(name: str) -> dict[str, Any]:
    with (WORKFLOWS / name).open("rb") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the ``on:`` block.

    PyYAML parses the bare key ``on`` as the boolean ``True`` (YAML 1.1
    truthiness), which is why this is not a plain ``workflow["on"]``.
    """
    for key in ("on", True):
        if key in workflow:
            block = workflow[key]
            return block if isinstance(block, dict) else dict.fromkeys(block or [])
    pytest.fail("workflow has no triggers")


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    return _load("ci.yml")


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    return _load("release.yml")


def test_release_has_no_independent_trigger(release: dict[str, Any]) -> None:
    """The release workflow must be reachable only by being called."""
    triggers = _triggers(release)
    assert "workflow_call" in triggers
    forbidden = {"push", "pull_request", "schedule", "workflow_run", "release"}
    assert not forbidden & set(triggers), (
        "release.yml gained an independent trigger; a commit that fails CI could "
        "now publish an image"
    )


def test_ci_release_job_depends_on_every_validation_job(ci: dict[str, Any]) -> None:
    jobs = ci["jobs"]
    assert "release" in jobs, "ci.yml no longer has the gating release job"
    needs = jobs["release"]["needs"]
    needs = {needs} if isinstance(needs, str) else set(needs)
    missing = REQUIRED_VALIDATION_JOBS - needs
    assert not missing, f"release job does not depend on validation jobs: {sorted(missing)}"


def test_ci_defines_every_required_validation_job(ci: dict[str, Any]) -> None:
    """A renamed job would satisfy ``needs`` while validating nothing."""
    assert set(ci["jobs"]) >= REQUIRED_VALIDATION_JOBS


def test_release_job_calls_the_release_workflow(ci: dict[str, Any]) -> None:
    assert ci["jobs"]["release"]["uses"] == "./.github/workflows/release.yml"


def test_release_never_publishes_from_a_pull_request(ci: dict[str, Any]) -> None:
    condition = " ".join(str(ci["jobs"]["release"]["if"]).split())
    assert "github.event_name != 'pull_request'" in condition
    assert "refs/heads/main" in condition
    assert "refs/tags/v" in condition


def test_ci_runs_on_tags_so_tagged_releases_are_also_gated(ci: dict[str, Any]) -> None:
    push = _triggers(ci)["push"]
    assert "v*" in push["tags"]
