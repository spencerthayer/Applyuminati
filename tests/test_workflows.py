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

PYPI_WORKFLOW = "pypi.yml"

#: Actions that upload to PyPI. Pinned to a major tag, never a floating branch.
PYPI_PUBLISH_ACTION = "pypa/gh-action-pypi-publish"


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


@pytest.fixture(scope="module")
def pypi() -> dict[str, Any]:
    path = WORKFLOWS / PYPI_WORKFLOW
    assert path.is_file(), (
        f"{PYPI_WORKFLOW} is missing; there is no way to install this project from PyPI at all"
    )
    return _load(PYPI_WORKFLOW)


def _needs(job: dict[str, Any]) -> set[str]:
    raw = job.get("needs", [])
    return {raw} if isinstance(raw, str) else set(raw)


def _ci_pypi_job(ci: dict[str, Any]) -> dict[str, Any]:
    """Return the ci.yml job that calls pypi.yml, whichever it is named."""
    callers = [
        job
        for job in ci["jobs"].values()
        if job.get("uses") == f"./.github/workflows/{PYPI_WORKFLOW}"
    ]
    assert len(callers) == 1, (
        f"ci.yml must call {PYPI_WORKFLOW} from exactly one job, found {len(callers)}; "
        "a second caller is a second, ungated path to uploading"
    )
    return callers[0]


def test_pypi_workflow_is_call_only(pypi: dict[str, Any]) -> None:
    """The PyPI workflow must be reachable only by being called."""
    triggers = _triggers(pypi)
    assert "workflow_call" in triggers
    forbidden = {"push", "pull_request", "schedule", "workflow_run", "release"}
    assert not forbidden & set(triggers), (
        "pypi.yml gained an independent trigger; a commit that fails CI could now upload to PyPI"
    )


def test_pypi_publish_input_defaults_to_false(pypi: dict[str, Any]) -> None:
    """A routine CI run must build, never upload."""
    publish = _triggers(pypi)["workflow_call"]["inputs"]["publish"]
    assert publish["type"] == "boolean"
    assert publish["default"] is False


def test_ci_pypi_job_depends_on_every_validation_job(ci: dict[str, Any]) -> None:
    missing = REQUIRED_VALIDATION_JOBS - _needs(_ci_pypi_job(ci))
    assert not missing, f"PyPI job does not depend on validation jobs: {sorted(missing)}"


def test_ci_publishes_to_pypi_only_from_a_pull_request_free_tag(ci: dict[str, Any]) -> None:
    """The caller's gate is a second line of defence behind the missing trigger."""
    condition = " ".join(str(_ci_pypi_job(ci)["if"]).split())
    assert "github.event_name != 'pull_request'" in condition
    assert "refs/heads/main" in condition or "refs/tags/v" in condition


def test_pypi_build_job_runs_before_the_upload(pypi: dict[str, Any]) -> None:
    jobs = pypi["jobs"]
    assert "build" in jobs
    assert "publish" in jobs
    assert "build" in _needs(jobs["publish"])


def test_pypi_publish_job_uses_trusted_publishing(pypi: dict[str, Any]) -> None:
    """OIDC only: a long-lived API token must never gate a release."""
    publish = pypi["jobs"]["publish"]
    assert publish["permissions"]["id-token"] == "write"
    assert publish["environment"] == "pypi"
    assert "password" not in publish.get("with", {}), "an API token is configured"


def test_ci_pypi_job_grants_the_oidc_permission_too(ci: dict[str, Any]) -> None:
    """A called workflow cannot widen the permissions it is handed.

    GitHub gives pypi.yml the intersection of the caller's grant and the
    callee's declaration, so ``id-token: write`` in pypi.yml alone is
    inert and the upload fails at the OIDC exchange. Both halves are needed,
    and the failure only shows up on the first real publish, which is the
    worst possible moment to discover it.
    """
    assert _ci_pypi_job(ci).get("permissions", {}).get("id-token") == "write", (
        "the ci.yml job calling pypi.yml does not grant id-token: write; the "
        "upload will fail at the OIDC exchange instead of at the first publish"
    )


def test_pypi_upload_step_is_gated_on_the_publish_input(pypi: dict[str, Any]) -> None:
    """No condition on the input means every CI run uploads to PyPI."""
    job = pypi["jobs"]["publish"]
    assert "inputs.publish" in " ".join(str(job.get("if", "")).split())
    uploads = [step for step in job["steps"] if PYPI_PUBLISH_ACTION in str(step.get("uses", ""))]
    assert len(uploads) == 1, f"expected exactly one upload step, found {len(uploads)}"
    # A major version tag, not a floating branch: @master can be repointed.
    assert uploads[0]["uses"].endswith("/v1"), f"unpinned publish action: {uploads[0]['uses']}"


def test_the_runtime_dependencies_include_what_the_code_imports() -> None:
    """A dependency dropped by accident installs cleanly and fails at runtime.

    This exists because `uvicorn[standard]` was removed from the dependency
    list by a scripted edit that rewrote the whole block. `applyuminati serve`
    imports it, so nothing local noticed until CI could not resolve the import.
    """
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    declared = " ".join(data["project"]["dependencies"])
    # Every third-party module the package imports at module scope.
    for module in (
        "aiosqlite",
        "alembic",
        "fastapi",
        "httpx",
        "pydantic",
        "pydantic-settings",
        "rich",
        "sqlalchemy",
        "structlog",
        "typer",
        "uvicorn",
        "websockets",
    ):
        assert module in declared, f"{module} is imported but not a declared dependency"
    # The asyncio extra is load-bearing: without greenlet the wheel installs and
    # then dies on the first import of applyuminati.db.
    assert "sqlalchemy[asyncio]" in declared
