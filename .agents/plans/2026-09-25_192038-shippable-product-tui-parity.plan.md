---
name: Shippable product, Textual TUI, and WebUI parity
overview: Make Applyuminati installable and genuinely usable by fixing the proven blocking defects (worker database lock, missing apply entry point, broken first run), adding a Textual TUI alongside the existing CLI, wiring the missing WebUI actions, and locking all three surfaces to one parity contract.
todos:
  - id: t01-fix-worker-lock
    content: Fix the worker database-is-locked deadlock
    status: completed
    dependencies: []
  - id: t02-add-apply-endpoint
    content: Add the start-an-application service, API route, and CLI command
    status: completed
    dependencies:
      - t01-fix-worker-lock
  - id: t03-link-attempt-to-application
    content: Propagate a terminal attempt outcome to the Application state
    status: completed
    dependencies:
      - t01-fix-worker-lock
  - id: t04-sync-sources-from-settings
    content: Call sync_from_settings on startup so config.toml works
    status: completed
    dependencies: []
  - id: t05-first-run-init
    content: Make init migrate, write config.toml, and provision auth
    status: completed
    dependencies:
      - t04-sync-sources-from-settings
  - id: t06-serve-web-dist-default
    content: Serve the built SPA without manual configuration
    status: completed
    dependencies: []
  - id: t07-accept-string-location
    content: Accept a string basics.location in JSON Resume import
    status: completed
    dependencies: []
  - id: t08-fix-uncoupled-ego-test
    content: Make the ego-lite health test environment-independent
    status: completed
    dependencies: []
  - id: t09-forward-job-state-filter
    content: Forward the dropped state filter on the jobs list route
    status: completed
    dependencies: []
  - id: t10-default-autonomous-submit
    content: Switch the default execution mode to autonomous_submit
    status: completed
    dependencies:
      - t02-add-apply-endpoint
  - id: t11-publish-to-pypi
    content: Add a PyPI build and publish workflow
    status: completed
    dependencies:
      - t05-first-run-init
      - t10-default-autonomous-submit
  - id: t12-rewrite-first-run-docs
    content: Rewrite the README quick start and SECURITY posture
    status: completed
    dependencies:
      - t11-publish-to-pypi
  - id: t13-tui-scaffold
    content: Scaffold the Textual package, deps, and import contracts
    status: completed
    dependencies:
      - t02-add-apply-endpoint
      - t06-serve-web-dist-default
  - id: t14-tui-jobs-screen
    content: Build the TUI Jobs screen with filtering
    status: completed
    dependencies:
      - t13-tui-scaffold
  - id: t15-tui-job-detail-apply
    content: Build the TUI job detail screen with the apply action
    status: completed
    dependencies:
      - t14-tui-jobs-screen
      - t10-default-autonomous-submit
  - id: t16-tui-needs-you-screen
    content: Build the TUI Needs You human-handoff screen
    status: completed
    dependencies:
      - t13-tui-scaffold
      - t03-link-attempt-to-application
  - id: t17-tui-dashboard-screen
    content: Build the TUI Dashboard screen
    status: completed
    dependencies:
      - t13-tui-scaffold
  - id: t18-tui-sources-settings-screen
    content: Build the TUI Sources and Settings screens
    status: completed
    dependencies:
      - t13-tui-scaffold
      - t04-sync-sources-from-settings
  - id: t19-tui-profile-screen
    content: Build the TUI Profile screen
    status: completed
    dependencies:
      - t13-tui-scaffold
  - id: t20-tui-headless-test-suite
    content: Add the headless TUI test suite
    status: completed
    dependencies:
      - t15-tui-job-detail-apply
      - t16-tui-needs-you-screen
      - t17-tui-dashboard-screen
      - t18-tui-sources-settings-screen
      - t19-tui-profile-screen
  - id: t21-web-missing-actions
    content: Expose discover, score, and apply in the WebUI
    status: completed
    dependencies:
      - t02-add-apply-endpoint
  - id: t22-web-applications-page
    content: Add the WebUI applications page and transitions
    status: completed
    dependencies:
      - t21-web-missing-actions
  - id: t23-web-source-options-and-prefs
    content: Add the WebUI source options form and profile preferences
    status: completed
    dependencies:
      - t04-sync-sources-from-settings
      - t21-web-missing-actions
  - id: t24-web-parity-tests
    content: Add WebUI tests for the newly exposed actions
    status: completed
    dependencies:
      - t22-web-applications-page
      - t23-web-source-options-and-prefs
  - id: t25-parity-contract
    content: Introduce the single-source parity manifest and its test
    status: completed
    dependencies:
      - t20-tui-headless-test-suite
      - t24-web-parity-tests
isProject: true
---

# Shippable Product, Textual TUI, and WebUI Parity

**Goal:** Make Applyuminati installable and usable end-to-end by a real user, add a Textual TUI that is at parity with the WebUI, and lock all three surfaces to one enforceable contract.

**Architecture:** `src/applyuminati/tui/` is a new top-layer package beside `cli`, `api`, and `host`. It calls `ServiceContainer` services in-process (no HTTP server required), exactly as the Typer CLI does today, and owns its own event loop through Textual. A new `src/applyuminati/surface_parity.py` becomes the single source of truth for which capabilities exist on which surface; a test asserts the CLI, API, TUI, and WebUI all match it.

**Tech Stack:** Python 3.12+, Textual `>=8.2,<9` (current stable 8.2.8, 2026-06-30), Typer, FastAPI, SQLAlchemy + aiosqlite, Alembic, React 19 + TanStack Query 5, Hatchling, uv, GitHub Actions + PyPI Trusted Publishing.

---

## 1. Goal and context

### What the investigation established

Every claim below was observed by running the system, not by reading it. Evidence is in §8.

**Works today, proven live:** Greenhouse discovery (200 real jobs, 194 created, 6 dedup-merged), Lever discovery, JSON Resume import, deterministic scoring, capability-driven browser selection, the HTTP API, the built SPA, and — when driven manually past the missing entry point — the apply engine itself, which selected Playwright, launched Chromium, reached a real Lever job, and paused in `waiting_for_human`.

**Blocking defect 1 — the apply worker can never write.** `TaskWorker.run_once` executes the whole task inside one `container.repositories()` unit of work. `claim_next` performs a conditional `UPDATE` and only `flush()`es it, so the SQLite write lock is held for the entire task. The handler then opens a *second* session (`attempt_tasks.py:243` calls `run_application_attempt(payload, context)` with `repos=None`), blocks for exactly `busy_timeout=10000`, and raises:

```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
[SQL: UPDATE application_attempts SET browser_backend=? ...]
```

Reproduced three times, including inside the real API server via its own lifespan worker. The attempt stays `pending` and is never persisted.

**Blocking defect 2 — nothing can start an application.** `AttemptService.create` (`services/attempt_service.py:119`) is the only attempt factory and has zero callers in `src/`. There is no `POST /api/v1/applications`, no `apply` CLI command, and no WebUI button. `browser-hosts/pair` is the only POST in the entire API that creates anything.

**First run is broken as documented.** `init` does not run migrations (`doctor` then dies on `no such table: profiles`), does not create the `config.toml` it reports creating, and the README never mentions the required password — without `APPLYUMINATI_SECURITY__PASSWORD` every endpoint returns 503. It also never mentions `web_dist`, so `/` 404s despite the README's "the UI and API are both there".

**`config.toml` is silently ignored.** `SourceService.sync_from_settings` (`services/source_service.py:145`) is defined and documented as the startup path for file-based source config, and is never called. No CLI flag sets source options either; only a raw API call works.

**Smaller real defects:** `ApplicationAttempt` never writes to `repos.applications`, so a confirmed submission never moves the `Application` to `SUBMITTED`. JSON Resume `basics.location` rejects a string (`core/models/jsonresume.py:50`) though the spec allows one. `GET /api/v1/jobs` accepts a `state` param and never forwards it (`api/routers/jobs.py:30,44`). `tests/test_ego_lite.py:334` asserts `"macOS" in detail`, which passes in CI (Linux) and fails on the maintainer's Mac.

**Surface gaps.** The WebUI has zero mutation actions for discover, score, apply, or applications, despite `useDiscover`, `useScore`, `useApplications`, `useApplication`, and `useTransitionApplication` already existing and unused in `apps/web/src/api/hooks.ts`. The CLI cannot set source options, has no `applications` group, and no `settings` group. There is no PyPI workflow — only a container image.

### Decisions taken (user-confirmed)

| Decision | Choice | Consequence recorded in this plan |
|---|---|---|
| TUI transport | **In-process `ServiceContainer`**, beside `cli`/`api`/`host` in the top import-linter layer | TUI needs no server, no port, no password. It **must not** call `asyncio.run` and **must** start the attempt worker itself. |
| Distribution | **Add PyPI publishing** (keep the container release untouched) | `pipx install applyuminati` / `uv tool install applyuminati` becomes the primary path. |
| Default execution mode | **`autonomous_submit`** | Reverses the posture in README §Security and SECURITY.md. See the risk note below. |

**Recorded risk, accepted by the user.** I flagged before the choice was made that defaulting to `autonomous_submit` contradicts the project's own documented posture ("Autonomous submission is opt-in", README line 178) and is a larger trust and support burden. The user selected it anyway. This plan executes that decision. To keep it honest rather than reckless:

- The safety machinery is **not** relaxed. The fabrication guard, the no-access-control-evasion rule, and the submission idempotency fingerprint all stay exactly as they are; `t10` changes one default value and the documentation, nothing else.
- README and SECURITY.md are rewritten to state the real default (t12). Leaving the old text would be a false claim.
- `tests/test_api.py:78` asserts `execution_mode == "research_only"` and must change with the behaviour.
- The TUI still shows the mode and the attempt's `WAITING_FOR_HUMAN` interventions prominently (t16), so the human handoff remains discoverable.

### Out of scope

Workday/other ATS drivers, company research, email monitoring, resume PDF rendering, vector memory, PostgreSQL/pgvector, and the other "Planned" README items. This plan makes what exists shippable; it does not add new ATS coverage.

---

## 2. Adaptive execution contract

Once implementation is authorized, repeat this loop until the authorized objective is verified or no useful in-scope action remains.

1. **Read and reconcile.** Read this plan on starting or resuming. Compare the checkpoint against the actual workspace and relevant external state. Preserve user changes. Pick a task whose dependencies are all `completed` and mark it `in_progress`. Treat the approach as revisable; preserve the objective and the constraints in §1.
2. **Check and record.** After each meaningful check, write into §8: timestamp, task and environment, expected result, actual observation, outcome (`pass` / `fail` / `inconclusive` / `not applicable`), and a sanitized evidence reference. A test that fails for the intended reason is a `pass` for that check. Record failures. Save before dependent work.
3. **Correct the plan.** When evidence contradicts the plan, record the old assumption, the finding, the revised approach, the affected task ids, and which checks to rerun. Update §4, the task bodies, the diagram, and §7 together. Do not leave stale instructions in force and do not erase the failed approach.
4. **Act within scope.** Make the smallest evidence-supported correction covered by the request. Routine local diagnosis and reversible fixes need no new permission. A revision does not authorize new external actions, destructive recovery, broader scope, or relaxed acceptance. If a correction crosses an authorization boundary, prepare the concrete proposed change, pause that action, and continue independent work.
5. **Revalidate.** Rerun the checks the correction affected. Reopen invalidated results and mark superseded evidence. Do not rerun unrelated checks, and never weaken an acceptance criterion to manufacture a pass. Historical actions (commits, published artifacts) are not undone by a status change and must not be blindly repeated.
6. **Checkpoint and continue.** Save statuses, dependencies, task bodies, the diagram, and §7 together. Before yielding or reporting completion, reconcile the plan with actual state and leave an exact next action.

If the same action fails again without new evidence, do not repeat it unchanged. Change the hypothesis or the diagnostic path and record that. If no supported path remains, record the blocker, continue independent tasks, and request only the missing input. A failed plan save is itself a checkpoint failure: restore the ability to record state before further mutations.

**Scope note for `t11`:** publishing to PyPI is an external, hard-to-reverse action. Build and validate the artifact locally and in CI; perform the actual publish only from a tagged release, and confirm with the user before the first live publish.

---

## 3. Execution checkpoint

| Field | Current state |
|---|---|
| Phase | **All 25 tasks complete and committed.** |
| Active task | None. |
| Last confirmed result | **584 Python tests passed, 0 failed, 0 errors.** Web: 77 tests, typecheck, lint, build clean. `ruff format --check` clean, `ruff check` clean, `pyright` 0 errors, `lint-imports` 4 kept. `make parity` regenerates `docs/parity.md` byte-identically. The manifest imports with Textual absent, verified in a clean no-extras venv. |
| Current approach | Complete. |
| Blockers / open decisions | None blocking. The first real PyPI publish needs a human: create the project, configure a Trusted Publisher against the `pypi` environment, and tag. 17 of 24 capabilities are missing on at least one surface; recorded in `docs/parity.md` as a product finding, not a failure. |

|---|---|---|
| `src/applyuminati/db/repositories/tasks.py` | Commit the claim and the lease reclaim | t01 |
| `src/applyuminati/tasks/worker.py` | Re-verify the claim/execute split; add a regression test hook | t01 |
| `src/applyuminati/services/attempt_service.py` | Add `start_for_job` | t02, t15, t21 |
| `src/applyuminati/api/routers/jobs.py` | `POST /api/v1/jobs/{job_id}/apply`; forward `state` | t02, t09 |
| `src/applyuminati/api/schemas.py` | `ApplyJobRequest`, `ApplyJobResponse` | t02 |
| `src/applyuminati/cli/main.py` | `applications` group, `tui` command, `--options` on `sources enable` | t02, t13 |
| `src/applyuminati/services/attempt_tasks.py` | Terminal outcome → `Application` transition | t03 |
| `src/applyuminati/services/container.py` | `sync_from_settings` on boot | t04 |
| `src/applyuminati/cli/main.py` (`init`) | Migrate, write config, set a password | t05 |
| `src/applyuminati/core/settings.py` | `web_dist` default; `ExecutionMode` default | t06, t10 |
| `src/applyuminati/core/models/jsonresume.py` | Accept `str` or `ResumeLocation` | t07 |
| `src/applyuminati/plugins/browsers/ego_lite.py` | Health detail invariant | t08 |
| `pyproject.toml` | `tui` extra, `rich` bump, import-linter, ruff ignores | t10, t13, t11 |
| `src/applyuminati/tui/**` (new) | The TUI package | t13–t20 |
| `src/applyuminati/surface_parity.py` (new) | The parity manifest | t25 |
| `apps/web/src/pages/**`, `apps/web/src/App.tsx` | Missing actions and routes | t21–t24 |
| `.github/workflows/pypi.yml` (new) | PyPI Trusted Publishing | t11 |
| `README.md`, `SECURITY.md` | Real first run, real default | t12 |

---

## 6. Tasks

Every task below is one `file:line`-anchored change with a full test-then-implement cycle. Use the project's own runners: `uv run pytest`, `uv run ruff check .`, `uv run pyright`, `uv run lint-imports`, and `cd apps/web && npm run test`.

**Baseline for every Python task.** `uv run pytest -q` currently reports `1 failed, 426 passed`. The single failure is `tests/test_ego_lite.py::test_a_backend_without_the_helper_is_not_selectable`, fixed by `t08`. Until then, treat that one failure as the known baseline, not a regression.

**Baseline for every WebUI task.** `cd apps/web && npm run lint && npm run typecheck && npm run test && npm run build` must be green before and after.

---

### t01-fix-worker-lock (DONE)

**Outcome (2026-09-26):** complete. AC-1 satisfied. Two deviations from the plan
text, both recorded in §7: the test had to go through `TaskWorker.run_once` rather
than call the handler directly, and it had to bind the process container to the
fixture's database. The sketch in this task below is superseded by
`tests/test_attempt_worker_persistence.py`; keep it as the shape, not the fixture.

**Shipped change:** `await self._session.commit()` in `claim_next` and in
`reclaim_expired_leases`, plus the module docstring. 12 insertions, 2 deletions
in one file.

**Objective:** Make the attempt worker able to persist, by committing the claim before the handler runs.

**Files:**
- Modify: `src/applyuminati/db/repositories/tasks.py:46-81` (`claim_next`), `:112-143` (`reclaim_expired_leases`)
- Modify: `src/applyuminati/tasks/worker.py:35-44` (docstring only if behaviour moves)
- Test: `tests/test_attempt_worker_persistence.py` (create)

**Step 1: Write the failing test.** This is the regression that matters most in the plan. It must fail with `database is locked` before the fix, not for an incidental reason.

```python
# tests/test_attempt_worker_persistence.py
"""The attempt worker must be able to write while its own task is claimed."""

from __future__ import annotations

import pytest

from applyuminati.core.models.execution import WorkflowState
from applyuminati.core.settings import ExecutionMode
from applyuminati.services.attempt_service import AttemptService
from applyuminati.services.attempt_tasks import (
    APPLICATION_ATTEMPT_KIND,
    ApplicationAttemptPayload,
)
from applyuminati.services.container import ServiceContainer
from applyuminati.tasks.handlers import TaskContext
from applyuminati.tasks.queue import TaskQueue
from applyuminati.tasks.worker import TaskWorker


async def test_worker_run_once_persists_the_attempt(
    container: ServiceContainer, seeded_application
) -> None:
    """AC-1: a claimed task must not hold a write lock that blocks the handler.

    Regression for the observed failure:
        sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
        [SQL: UPDATE application_attempts SET browser_backend=? ...]
    """
    application, job = seeded_application

    async with container.repositories() as repos:
        profile = await repos.profiles.get_active()
        attempt = await AttemptService(repos).create(
            application_id=application.id,
            job=job,
            profile=profile,
            mode=ExecutionMode.RESEARCH_ONLY,
        )
        await TaskQueue(repos.tasks).submit(
            APPLICATION_ATTEMPT_KIND,
            {"attempt_id": attempt.id},
            idempotency_key=f"test:{attempt.id}",
        )

    async with container.repositories() as repos:
        worker = TaskWorker(TaskQueue(repos.tasks))
        did_work = await worker.run_once(kinds=[APPLICATION_ATTEMPT_KIND])

    assert did_work is True

    async with container.read_repositories() as repos:
        reloaded = await AttemptService(repos).get(attempt.id)
        assert reloaded.workflow_state is not WorkflowState.PENDING
```

The `container` and `seeded_application` fixtures must use a **file-backed** SQLite database in a `tmp_path`, not `:memory:`. An in-memory database with `StaticPool` shares one connection and therefore cannot reproduce a cross-connection lock; using it would make the test pass before the fix and prove nothing. Reuse the existing `settings`/`database` fixture pattern in `tests/conftest.py` but point `db_path` at a temporary file.

**Step 2: Run the test to verify it fails.**

Run: `uv run pytest tests/test_attempt_worker_persistence.py -v`

Expected: FAIL with `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked`. If it fails for any other reason — an import error, a missing fixture, a `StaticPool` in-memory database — the test is wrong; fix the test before touching the source. Confirm the failure mentions `database is locked`.

**Step 3: Make the minimal change.** Claiming a task is a durable state transition: it is what makes the lease meaningful, so it must be committed. Add a commit after the successful conditional `UPDATE` in `claim_next`, and after the reclaim loop.

In `src/applyuminati/db/repositories/tasks.py`, inside `claim_next`, immediately after the `rowcount == 0` early-return branch and the `row = await self._session.get(TaskRow, task_id)` read, add:

```python
        # The claim is a durable transition, not scratch state: it is what makes
        # the lease meaningful, so a crash after this point is recovered rather
        # than silently un-claimed. Committing here also releases the SQLite
        # write lock before the handler opens its own session, which is what
        # previously deadlocked the worker against itself.
        await self._session.commit()
        return row_to_task(row) if row else None
```

Replace the existing final line `return row_to_task(row) if row else None` with the block above. In `reclaim_expired_leases`, add `await self._session.commit()` after `await self._session.flush()` at line 142, for the same reason.

Update the module docstring at `db/repositories/tasks.py:3-7` to state that the claim commits, and why.

**Step 4: Run the test to verify it passes.**

Run: `uv run pytest tests/test_attempt_worker_persistence.py -v`

Expected: PASS.

**Step 5: Run the full suite for regressions.**

Run: `uv run pytest -q`

Expected: `1 failed, 427 passed` — the same known `test_ego_lite` baseline, plus the new test. Any other failure is a regression from this change; investigate before continuing.

**Step 6: Verify the real end-to-end path, not just the unit.**

Run a throwaway script in a `tmp_path` data dir that enables `lever` against a real public tenant, discovers, scores, creates an attempt via `AttemptService.create`, submits the task, and runs `TaskWorker.run_once`. Expected: the attempt leaves `pending`, and the log shows no `worker.task_crashed`. Mark the script throwaway and delete it once it passes; do not commit it.

---

### t02-add-apply-endpoint

**Objective:** Give a user a way to start an application from the API and the CLI.

**Files:**
- Modify: `src/applyuminati/services/attempt_service.py` (add `start_for_job`)
- Modify: `src/applyuminati/api/routers/jobs.py` (add the route)
- Modify: `src/applyuminati/api/schemas.py` (add request/response models)
- Modify: `src/applyuminati/cli/main.py` (add the `applications` group)
- Test: `tests/test_apply_entry_point.py` (create)

**Step 1: Write the failing tests.**

```python
# tests/test_apply_entry_point.py
"""A user must be able to start an application. Nothing else may create one."""

from __future__ import annotations

import pytest

from applyuminati.api.app import create_app
from applyuminati.core.errors import ApplyuminatiError


async def test_start_application_enqueues_exactly_one_attempt(
    container, seeded_application
) -> None:
    application, job = seeded_application
    async with container.repositories() as repos:
        profile = await repos.profiles.get_active()
        attempt = await AttemptService(repos).start_for_job(
            job_id=job.id, profile=profile, mode=ExecutionMode.RESEARCH_ONLY
        )
        assert attempt.job_id == job.id
        tasks, _ = await repos.tasks.list()
        assert [t.kind for t in tasks] == [APPLICATION_ATTEMPT_KIND]


async def test_start_application_is_idempotent_per_job(container, seeded_application) -> None:
    """A second start must not enqueue a second concurrent attempt."""
    application, job = seeded_application
    async with container.repositories() as repos:
        profile = await repos.profiles.get_active()
        first = await AttemptService(repos).start_for_job(
            job_id=job.id, profile=profile, mode=ExecutionMode.RESEARCH_ONLY
        )
        with pytest.raises(ApplyuminatiError) as exc:
            await AttemptService(repos).start_for_job(
                job_id=job.id, profile=profile, mode=ExecutionMode.RESEARCH_ONLY
            )
        assert "in progress" in str(exc.value).lower()
```

Also add an API test asserting `POST /api/v1/jobs/{job_id}/apply` returns 201 with an attempt id, and 409 when an attempt is already in flight for that job.

**Step 2: Run to verify failure.**

Run: `uv run pytest tests/test_apply_entry_point.py -v`

Expected: FAIL — `AttributeError: 'AttemptService' object has no attribute 'start_for_job'`. Confirm the failure is that missing method, not a fixture problem.

**Step 3: Implement the service method.**

Add to `AttemptService` in `src/applyuminati/services/attempt_service.py`, beside `create`:

```python
async def start_for_job(
    self,
    *,
    job_id: str,
    profile: CareerProfile | None,
    mode: ExecutionMode,
) -> ApplicationAttempt:
    """Create the attempt for a job and queue it. The only user entry point.

    Job-centric because that is what a user selects. The Application row is
    ensured rather than required, so a job that has not been scored can
    still be applied to. Re-entry is refused while an attempt for the same
    job is still in flight, which is what keeps the idempotency fingerprint
    meaningful.
    """
    job = await self._repos.jobs.get(job_id)
    if job is None:
        raise NotFoundError(f"job {job_id} not found", code="resource_gone.job")
    if profile is None:
        profile = await self._repos.profiles.get_active()
    if profile is None:
        raise ConfigurationError(
            "import a career profile before applying",
            code="configuration.profile_missing",
        )

    existing = await self._repos.attempts.active_for_job(job_id)
    if existing is not None:
        raise ConflictError(
            f"an application for this job is already in progress ({existing.workflow_state.value})",
            code="conflict.attempt_in_flight",
        )

    application = await self._repos.applications.ensure(job_id, profile.id)
    attempt = await self.create(application_id=application.id, job=job, profile=profile, mode=mode)
    await self.enqueue_resume(attempt)
    return attempt
```

This depends on two members that may not exist yet — check and add them if absent:

- `TaskRepository`-backed `AttemptRepository.active_for_job(job_id)`: return the newest attempt for the job whose `workflow_state` is in `WORKFLOW_TERMINAL`-complement, i.e. not `COMPLETED`, `FAILED`, `ABANDONED`, or `SKIPPED`. Mirror the state set already defined in `core/models/execution.py`.
- `ConflictError` in `core/errors.py`, mapped to HTTP 409 in `api/mappers.py` / the error handler. If the project prefers an existing error with a `recovery` attribute, use that and record the choice in §8.

**Step 4: Add the API route** in `src/applyuminati/api/routers/jobs.py`, mirroring the existing `discover_jobs` style (query params, `Depends(get_container_dep)`):

```python
@router.post("/{job_id}/apply", response_model=ApplyJobResponse, status_code=201)
async def apply_to_job(
    job_id: str,
    request: ApplyJobRequest,
    container: ServiceContainer = Depends(get_container_dep),
) -> ApplyJobResponse:
    async with container.repositories() as repos:
        svc = AttemptService(repos)
        try:
            attempt = await svc.start_for_job(job_id=job_id, profile=None, mode=request.mode)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        except ConfigurationError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc
    return ApplyJobResponse(attempt_id=attempt.id, state=attempt.workflow_state.value)
```

`ApplyJobRequest` carries `mode: ExecutionMode = ExecutionMode.AUTONOMOUS_SUBMIT` — the resolved default after `t10`, so do not hardcode a literal that `t10` will change. `ApplyJobResponse` carries `attempt_id: str` and `state: str`.

**Step 5: Add the CLI group** in `src/applyuminati/cli/main.py`. Use the existing `_run_async` helper (`cli/main.py:36`) and the same Typer table style as `jobs_app`:

```python
applications_app = typer.Typer(help="Start and inspect applications.")
app.add_typer(applications_app, name="applications")


@applications_app.command("apply")
def applications_apply(
    job_id: str = typer.Argument(..., help="Job id to apply to"),
    mode: str = typer.Option("autonomous_submit", help="research_only or autonomous_submit"),
) -> None:
    """Start an application for a job. This is the only way to begin applying."""
    ...
```

Also add `applications list` and `applications show <id>` so the CLI can see what it started. Do not add a `transition` command; the state machine advances on its own and a manual override invites corruption.

**Step 6: Run the tests.**

Run: `uv run pytest tests/test_apply_entry_point.py -v` then `uv run pytest -q`

Expected: new tests PASS; suite is `1 failed` (known `test_ego_lite`) and otherwise green.

**Step 7: Manual smoke.** `uv run applyuminati applications apply <job_id>` against a discovered job in a throwaway data dir. Expected: an attempt id printed, and within a few seconds the attempt leaves `pending`. Record the observed state in §8.

---

### t03-link-attempt-to-application

**Objective:** Make a terminal attempt outcome move the `Application`, so a confirmed submission shows as `SUBMITTED`.

**Files:**
- Modify: `src/applyuminati/services/attempt_tasks.py:74-125` (`_run`)
- Test: `tests/test_attempt_application_link.py` (create)

**Step 1: Write the failing test.**

```python
async def test_confirmed_submission_marks_the_application_submitted(
    container, seeded_application, faked_driver
) -> None:
    """An attempt that reaches SUBMISSION_CONFIRMED must move its Application."""
    application, job = seeded_application
    async with container.repositories() as repos:
        profile = await repos.profiles.get_active()
        attempt = await AttemptService(repos).start_for_job(
            job_id=job.id, profile=profile, mode=ExecutionMode.AUTONOMOUS_SUBMIT
        )

    # run the worker with the driver stubbed to CONFIRMED
    ...

    async with container.read_repositories() as repos:
        reloaded = await repos.applications.get(application.id)
        assert reloaded.state is ApplicationState.SUBMITTED
```

Model the stub on the existing `faked_driver` fixtures in `tests/test_execution.py`; reuse them rather than inventing a second fake.

**Step 2: Verify failure.** `uv run pytest tests/test_attempt_application_link.py -v`

Expected: FAIL — the application is still `SHORTLISTED` or `PREPARING`.

**Step 3: Implement.** In `services/attempt_tasks.py::_run`, after `run_step` returns and before building `result`, add a single transition through the existing machine, mapping terminal workflow states onto application states:

- `WorkflowState.SUBMISSION_CONFIRMED` → `ApplicationState.SUBMITTED`
- `WorkflowState.ABANDONED` → `ApplicationState.SKIPPED`
- `WorkflowState.FAILED` → leave the application where it is; the attempt carries the failure detail

Use `ApplicationMachine.transition` with `actor=ActorKind.SYSTEM` so the event log records the cause, exactly as `scoring_service.py:185` does. Wrap the transition in `try/except ApplyuminatiError` and log a warning on rejection, matching `scoring_service.py:199-206` — a rejected transition must not lose the attempt.

**Step 4: Verify.** `uv run pytest tests/test_attempt_application_link.py -v` then the full suite. Expected: PASS; no new failures.

---

### t04-sync-sources-from-settings

**Objective:** Make `config.toml` authoritative for source enablement and options, as its own docstring promises.

**Files:**
- Modify: `src/applyuminati/services/container.py:86-104` (`ServiceContainer.__init__`) or the API lifespan
- Test: `tests/test_settings_sync.py` (create)

**Step 1: Write the failing test.** Write a `config.toml` into a `tmp_path` data dir declaring a source enabled with options, build the container, and assert the repository reflects it.

```python
async def test_config_file_configures_sources(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(
        "[discovery.sources.greenhouse]\nenabled = true\n\n"
        '[discovery.sources.greenhouse.options]\nboards = ["example"]\n'
    )
    settings = Settings(data_dir=tmp_path)
    container = ServiceContainer(settings)
    async with container.repositories() as repos:
        svc = SourceService(repos, settings)
        views = {v.slug: v for v in await svc.list(probe_health=False)}
    assert views["greenhouse"].enabled is True
    assert views["greenhouse"].options == {"boards": ["example"]}
```

**Step 2: Verify failure.** Expected: FAIL — the source is not enabled, because `sync_from_settings` is never called.

**Step 3: Implement.** The sync needs a repository, and `__init__` is synchronous, so do not call it there. Call it in the async startup path instead — the API lifespan (`api/app.py:42`) already opens repositories for `browser_hosts_reset`; add the sync there. For the CLI and the TUI, expose a helper in `services/container.py`:

```python
async def sync_settings_to_db(self) -> int:
    """Apply settings.discovery.sources to the database. Call on startup.

    Config files are how a Docker or pip install configures sources, so the
    file must win for any source it names. Idempotent.
    """
    async with self.repositories() as repos:
        return await SourceService(repos, self.settings).sync_from_settings()
```

Call it from the API lifespan and from the TUI's `on_mount` (`t13`). Do not call it from every CLI command: that would silently re-enable a source the user just disabled with `sources disable`. The documented rule is "the file is authoritative for any source it mentions", which `sync_from_settings` already implements — preserve that.

**Step 4: Verify.** `uv run pytest tests/test_settings_sync.py -v`, then the full suite.

**Step 5: Manual check.** With the test's `config.toml` in a data dir, run `applyuminati sources list`. Expected: Greenhouse shows enabled with the configured boards, and `doctor` reports it healthy rather than `no boards configured`.

---

### t05-first-run-init

**Objective:** One command takes a new install to a working state.

**Files:**
- Modify: `src/applyuminati/cli/main.py:47-63` (`init`)
- Test: `tests/test_first_run.py` (create)

**Step 1: Write the failing test.**

```python
def test_init_leaves_a_migrated_database_and_a_real_config(tmp_path) -> None:
    """AC-3: after init, the schema exists and config.toml is on disk."""
    # run `init` with APPLYUMINATI_DATA_DIR pointed at tmp_path
    ...
    assert (tmp_path / "applyuminati.db").exists()
    assert (tmp_path / "config.toml").exists()
    # and the schema is migrated
    assert table_exists(tmp_path / "applyuminati.db", "profiles")
```

Also assert the printed next-steps text matches what actually happened, so the banner cannot drift into lying again.

**Step 2: Verify failure.** Expected: FAIL — no `config.toml`, and `profiles` does not exist.

**Step 3: Implement.** Rewrite `init` to, in order:

1. `settings.ensure_directories()`
2. Run migrations. Do not shell out to `alembic` from the CLI — import the Alembic config programmatically via `alembic.config.Config` / `command.upgrade` against `settings.db_path`, wrapped in `_run_async` if needed, or run the synchronous `command.upgrade` directly since it is not async.
3. Write a real `config.toml` if one is not already present, as a commented template with the `[discovery.sources.*]` and `[security]` sections. Never overwrite an existing file.
4. If `settings.security.password` is unset, generate a strong random password, set it, print it once with a clear instruction to change it, and tell the user how. If it is already set, leave it alone.
5. Print next steps that are true: what was done, the URL, and the password if just generated.

Decide and record in §8 whether `init` may prompt. A prompt breaks scripted installs; prefer generating and printing, with `--password` to override.

**Step 4: Verify.** `uv run pytest tests/test_first_run.py -v`, then the full suite. Then a real clean run:

```bash
export APPLYUMINATI_DATA_DIR=$(mktemp -d)
uv run applyuminati init
uv run applyuminati doctor     # must NOT raise "no such table: profiles"
```

Expected: `doctor` renders its tables with `Schema` populated and `Profile: not imported`. Record the output in §8.

---

### t06-serve-web-dist-default

**Objective:** `applyuminati serve` serves the UI with no extra configuration.

**Files:**
- Modify: `src/applyuminati/core/settings.py:256-257` (`web_dist`)
- Test: `tests/test_web_dist.py` (create)

**Step 1: Write the failing test.** Build the app with a settings object whose `web_dist` is unset but whose `apps/web/dist` exists, and assert `GET /` returns 200 with HTML.

**Step 2: Verify failure.** Expected: 404, matching the observed behaviour.

**Step 3: Implement.** Change the default resolution rather than the default value: keep `web_dist: Path | None = None` as "auto", and in `api/app.py:140-145`, when it is `None`, fall back to `<repo>/apps/web/dist` and then to the installed package data. If neither exists, keep the current warning and continue serving the API only — a headless server install must not crash. Add a `validate` step to `init` that prints "run `npm run build` in apps/web" when the fallback directory is absent.

**Step 4: Verify.** `uv run pytest tests/test_web_dist.py -v`. Then start the server and `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/` — expected 200.

---

### t07-accept-string-location

**Objective:** Import JSON Resume files that use the spec's allowed string form for `basics.location`.

**Files:**
- Modify: `src/applyuminati/core/models/jsonresume.py:22-50`
- Test: `tests/test_jsonresume.py` (modify if it exists, else create)

**Step 1: Write the failing test.**

```python
def test_import_accepts_a_plain_string_location() -> None:
    doc = {
        "basics": {"name": "Test", "label": "Engineer", "location": "Austin, TX"},
        "skills": [],
    }
    resume = JsonResume.model_validate(doc)  # must not raise
    assert resume.basics is not None
    assert resume.basics.location is not None
```

**Step 2: Verify failure.** Expected: `ValidationError: basics.location — Input should be a valid dictionary or instance of ResumeLocation`.

**Step 3: Implement.** Add a Pydantic field validator that normalises a string into a `ResumeLocation` with `raw`/`address` set, keeping the object form working unchanged:

```python
class Basics(BaseModel):
    model_config = _CFG
    name: str | None = None
    label: str | None = None
    email: str | None = None
    location: ResumeLocation | None = None

    @field_validator("location", mode="before")
    @classmethod
    def _accept_string_location(cls, value: object) -> object:
        """The JSON Resume schema allows a plain string here. Accept both."""
        if isinstance(value, str):
            return {"address": value}
        return value
```

Confirm `ResumeLocation` has a field that holds the free-text value; if it only has structured parts, add a `raw: str | None` field and set it. Check how the importer already renders `basics.location` downstream so the string still reaches the profile.

**Step 4: Verify.** Run the new test and the existing resume tests. Expected: PASS.

---

### t08-fix-uncoupled-ego-test

**Objective:** Make `tests/test_ego_lite.py` pass on both macOS and Linux.

**Files:**
- Modify: `tests/test_ego_lite.py:320-340`
- Modify: `src/applyuminati/plugins/browsers/ego_lite.py` only if the detail string must change

**Step 1: Understand the failure.** The test is named `test_a_backend_without_the_helper_is_not_selectable` and asserts both `not report.usable` and `"macOS" in report.detail`. On Linux the helper is absent, so the detail names the platform. On this maintainer's Mac the helper **is** installed at `~/.local/bin/ego-browser` but fails its smoke marker, producing `... ran but did not echo the smoke marker`. Both are legitimately unusable; only the message differs.

**Step 2: Fix the assertion to test the invariant, not the wording.** The behaviour under test is "a backend that cannot pass its smoke check is not selectable". Assert that, and assert the platform is named only when the failure is a missing binary. Delete the `"macOS" in report.detail` assertion rather than re-pinning it to the other message — asserting on an error string couples the test to wording that is not the behaviour.

**Step 3: Verify.** `uv run pytest tests/test_ego_lite.py -v` on this machine. Expected: PASS. The full suite should now be fully green — that is the new baseline for `t09` onward.

---

### t09-forward-job-state-filter

**Objective:** Honour the `state` query parameter on `GET /api/v1/jobs`.

**Files:**
- Modify: `src/applyuminati/api/routers/jobs.py:30,44`
- Test: the existing API jobs tests, or `tests/test_api.py` (modify)

**Step 1: Write the failing test.** Request `GET /api/v1/jobs?state=shortlisted` and assert only shortlisted jobs come back. Expected: FAIL — the filter is ignored.

**Step 2: Fix.** Pass `states=state or None` into `JobService.list(...)` at line 44, matching the signature already used by `ApplicationService.list` (`application_service.py:34-47`).

**Step 3: Verify.** The new test, then `uv run pytest -q`. Expected: green.

---

### t10-default-autonomous-submit

**Objective:** Make `autonomous_submit` the shipped default, as the user decided.

**Files:**
- Modify: `src/applyuminati/core/settings.py` (`ExecutionMode` field default)
- Modify: `tests/test_api.py:78`
- Test: `tests/test_execution_mode.py` (create)

**Step 1: Write the failing test.**

```python
async def test_default_execution_mode_is_autonomous_submit(tmp_path) -> None:
    assert Settings(data_dir=tmp_path).execution_mode is ExecutionMode.AUTONOMOUS_SUBMIT
```

**Step 2: Verify failure.** Expected: FAIL, `RESEARCH_ONLY`.

**Step 3: Change the default** in `core/settings.py`. Change exactly one default. **Do not touch** the fabrication guard, the CAPTCHA/login-wall detection, the `guard_duplicate` fingerprint, or anything in `applications/runner.py`. Those are the safety net and this task changes none of them.

**Step 4: Update the existing assertion** at `tests/test_api.py:78` from `"research_only"` to `"autonomous_submit"`. This is a behaviour change the user asked for, so updating the test is correct here; record it in §8 as a deliberate, user-directed change rather than a weakened test.

**Step 5: Verify.** `uv run pytest -q`. Expected: green. Confirm the driver tests in `tests/test_drivers.py`, which already exercise `AUTONOMOUS_SUBMIT` extensively, still pass.

**Step 6: Record the decision** in §8 with the date, the user's explicit choice, and the note that the README and SECURITY posture updates are tracked in `t12`.

---

### t11-publish-to-pypi

**Objective:** `pipx install applyuminati` yields a working CLI and TUI.

**Files:**
- Create: `.github/workflows/pypi.yml`
- Modify: `pyproject.toml` (project URLs, classifiers, keywords, `dynamic` if needed)
- Test: `tests/test_workflows.py` (modify — it already asserts workflow invariants)
- Modify: `Makefile` (add a `dist` target)

**Step 1: Write the failing test.** `tests/test_workflows.py` already parses `.github/workflows/*` and asserts invariants. Add a test that a PyPI workflow exists, is gated on the validation jobs, uses Trusted Publishing (`pypa/gh-action-pypi-publish` with `id-token: write`), and has **no** trigger that can reach it without CI passing. Mirror the reasoning already written at the top of `.github/workflows/release.yml` about `workflow_call` gating.

**Step 2: Verify failure.** Expected: FAIL, no `pypi.yml`.

**Step 3: Implement the workflow.**

```yaml
name: PyPI

on:
  workflow_call:
    inputs:
      publish:
        description: "Upload to PyPI. False builds and validates only."
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv build
      - uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/

  publish:
    needs: [build]
    if: ${{ inputs.publish }}
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write   # Trusted Publishing
    steps:
      - uses: actions/download-artifact@v4
        with: { name: dist, path: dist/ }
      - uses: pypa/gh-action-pypi-publish@release/v1
```

Wire it into `ci.yml` behind `needs: [python, web, docker]`, defaulting `publish: false`, and enable it on tag pushes only after a Trusted Publisher is configured on the PyPI project. Do not add a `push:` trigger — the existing `release.yml` header explains at length why that breaks the guarantee, and the same reasoning applies here.

**Step 4: Verify the artifact locally.**

```bash
uv build
uv run twine check dist/*        # or: uv run python -m twine check dist/*
```

Expected: both wheels and the sdist pass metadata checks. Then install the wheel into a throwaway venv and run `applyuminati --help` and `applyuminati tui --help`. Record the result in §8.

**Step 5: Confirm the wheel contains the TUI.** Once `t13` exists, verify `src/applyuminati/tui/*.tcss` is packaged. Hatchling includes package data under `packages = ["src/applyuminati"]` by default; confirm rather than assume, because a missing `.tcss` produces a TUI that starts and then renders unstyled.

**Step 6: Stop before publishing.** Record in §8 that the build is validated and the live publish is pending the user's go-ahead plus a configured Trusted Publisher.

---

### t12-rewrite-first-run-docs

**Objective:** The README tells the truth about install, first run, and the execution-mode default.

**Files:**
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `docs/ats-roadmap.md` only if the driver maturity labels change

**Step 1: Rewrite Quick start** to the sequence that actually works, verified in `t05`:

```bash
uv tool install applyuminati        # or: pipx install applyuminati
applyuminati init
applyuminati tui                    # or: applyuminati serve
```

Remove the manual `uv run alembic upgrade head` step if `t05` made `init` migrate, and remove the claim that `/` serves the UI unless `t06` landed. Keep the provider-configuration section; it is accurate.

**Step 2: Correct the status section.** The README currently says "This is the foundation PR" and lists the WebUI as complete. Update "Current implementation status" to state what now works, including a Textual TUI, and to correct any claim that outruns the code.

**Step 3: Correct the security posture** to match `t10`: autonomous submission is now the default, not opt-in. State plainly what the system still refuses to do — no CAPTCHA solving, no login-wall circumvention, no fabricated facts — since those guarantees are unchanged. Do not soften them.

**Step 4: Add a TUI section** with a screenshot placeholder and the key bindings.

**Step 5: Verify.** Run every command in the new Quick start on a clean data dir and paste the real output into the README. A README whose quick start has not been executed is the exact failure this task exists to fix.

---

### t13-tui-scaffold

**Objective:** A `applyuminati tui` command that starts a working Textual app with no server running.

**Files:**
- Create: `src/applyuminati/tui/__init__.py`
- Create: `src/applyuminati/tui/app.py`
- Create: `src/applyuminati/tui/styles.tcss`
- Create: `src/applyuminati/tui/context.py` (shared service handles)
- Modify: `pyproject.toml` (extra, `rich` bump, import-linter, ruff ignores)
- Modify: `src/applyuminati/cli/main.py` (the `tui` command)
- Test: `tests/test_tui_smoke.py` (create)

**Step 1: Add the dependency and config.**

In `pyproject.toml`, extend `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
browser = ["playwright>=1.49"]
tui = ["textual>=8.2,<9"]
```

Bump `rich>=13.9` to `rich>=14.2` in the base `dependencies` — Textual 8.2.8 requires `rich>=14.2.0`, and the current pin would resolve to a version Textual cannot use. Run `uv lock` and confirm the resolver picks a compatible pair.

Add the TUI to the top layer of the import-linter contract at `pyproject.toml:217`:

```toml
  "applyuminati.cli | applyuminati.api | applyuminati.host | applyuminati.tui",
```

Add a ruff per-file ignore mirroring the CLI's, since the TUI constructs option defaults the same way:

```toml
"src/applyuminati/tui/**" = ["T20", "B008", "ASYNC240"]
```

Run `uv run lint-imports` and `uv run ruff check .`. Expected: clean.

**Step 2: Write the failing smoke test.**

```python
async def test_tui_app_starts_headless() -> None:
    from applyuminati.tui.app import TuiApp

    app = TuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#content") is not None
```

Run: `uv run pytest tests/test_tui_smoke.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'applyuminati.tui'`. `asyncio_mode = "auto"` is already set at `pyproject.toml:193` and `pytest-asyncio>=0.25` is already in the dev group, so no test-config change is needed — confirm rather than assume.

**Step 3: Implement the app shell.**

The two hard constraints, both learned from the Textual 8.2.8 sources:

- **Textual owns the event loop.** `App.run()` calls `asyncio.run()` or `run_until_complete()` on the global loop. The CLI helper `_run_async` (`cli/main.py:36`) is `asyncio.run` and **must not be used anywhere in `tui/`**. Every service call goes through `@work(exclusive=True) async def`, which runs on Textual's loop with no thread hop.
- **The TUI must start the attempt worker itself.** The API starts it in its lifespan; a TUI has no lifespan. Start it in `on_mount` and stop it in `on_unmount`.

```python
# src/applyuminati/tui/app.py
"""Terminal UI. Talks to services in-process, like the CLI does.

Textual owns the event loop, so nothing here may call asyncio.run — the CLI
helper _run_async is forbidden in this package. Long operations are Textual
workers. The attempt worker is started here because, unlike the API, there is
no lifespan to start it in.
"""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Static

from applyuminati.core.settings import get_settings
from applyuminati.services.container import ServiceContainer, get_container

_CSS_PATH = "styles.tcss"


class TuiApp(App[None]):
    """Root application. Subclasses mount concrete screens."""

    TITLE = "Applyuminati"
    CSS_PATH = _CSS_PATH

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("question_mark", "show_help", "Help"),
    ]

    def __init__(self, container: ServiceContainer | None = None) -> None:
        super().__init__()
        self._container = container
        self._worker_stop: asyncio.Event | None = None
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def container(self) -> ServiceContainer:
        if self._container is None:
            self._container = get_container()
        return self._container

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="content"):
            yield Static("Applyuminati", id="placeholder")
        yield Footer()

    async def on_mount(self) -> None:
        settings = get_settings()
        settings.ensure_directories()
        await self.container.sync_settings_to_db()
        self._worker_stop = asyncio.Event()
        from applyuminati.services.attempt_tasks import run_attempt_worker_forever

        self._worker_task = asyncio.create_task(
            run_attempt_worker_forever(poll_interval=1.0, stop_event=self._worker_stop),
            name="tui-attempt-worker",
        )

    async def on_unmount(self) -> None:
        if self._worker_stop is not None:
            self._worker_stop.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        await self.container.aclose()
```

Add `src/applyuminati/tui/styles.tcss` with a `Screen { layout: vertical; }` baseline and a docked toolbar. Use `1fr`, not `100%`, for the remaining container — docked `Header`/`Footer` are out of flow, and `height: 100%` on a sibling overflows.

**Step 4: Add the CLI command.**

```python
@app.command()
def tui() -> None:
    """Open the terminal UI. No server required."""
    from applyuminati.tui.app import TuiApp

    TuiApp().run()
```

Import lazily inside the command so a headless install without the `tui` extra still gets a working CLI, and the failure is a clear message rather than an import error at startup.

**Step 5: Verify.** `uv run pytest tests/test_tui_smoke.py -v` → PASS. Then, with the `tui` extra synced, run `uv run applyuminati tui` and confirm it renders a header, a content area, and a footer. Capture a screenshot as evidence. Confirm `applyuminati --help` still works with the extra absent.

---

### t14-tui-jobs-screen

**Objective:** A browsable, filterable job list.

**Files:**
- Create: `src/applyuminati/tui/screens/jobs.py`
- Create: `src/applyuminati/tui/widgets.py` (shared `JobTable` if it earns its place)
- Modify: `src/applyuminati/tui/app.py` (screen switch)
- Modify: `src/applyuminati/tui/styles.tcss`
- Test: `tests/test_tui_jobs.py` (create)

**Step 1: Write the failing test.**

```python
async def test_jobs_screen_lists_rows_from_the_service(container, seeded_jobs) -> None:
    from applyuminati.tui.app import TuiApp

    app = TuiApp(container)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#jobs-table", DataTable)
        assert table.row_count == len(seeded_jobs)
```

**Step 2: Verify failure.** Expected: FAIL, no `#jobs-table`.

**Step 3: Implement.** Two Textual specifics that will otherwise waste time:

- `DataTable` defaults to `cursor_type="cell"`, and `RowSelected` **only fires when `cursor_type="row"`**. Construct it as `DataTable(id="jobs-table", cursor_type="row")` or handle `CellHighlighted` instead.
- `add_row` raises `ValueError` if given more cells than declared columns, so call `add_columns` first.

Load data in a worker, never in `on_mount` directly:

```python
    @work(exclusive=True, exit_on_error=False)
    async def load_jobs(self, query: str = "") -> None:
        table = self.query_one("#jobs-table", DataTable)
        async with self.app.container.read_repositories() as repos:
            svc = JobService(repos, self.app.container.settings)
            items, total = await svc.list(query=query or None, limit=200)
        table.clear()
        for view in items:
            table.add_row(
                view.job.company,
                view.job.title[:48],
                (view.job.locations[0].display() if view.job.locations else "-")[:28],
                f"{view.score.overall:.2f}" if view.score else "-",
                view.application_state or "-",
                key=view.job.id,
            )
```

Check `JobService.list`'s actual return shape and `JobView` field names before writing this; the audit recorded `JobService` as list/get with no create/apply. Store the job id in the row `key` so selection needs no second lookup.

Use `exit_on_error=False` for background refreshes — the default `True` kills the whole app on any worker exception, which is wrong for a polling refresh.

Add a search `Input` above the table and re-run `load_jobs` on `Input.Changed` with `exclusive=True`, which is the debounce primitive. Note that a focused `Input` swallows printable keys, so do not bind bare letters as app-wide actions; use modifier keys or `priority=True`.

**Step 4: Verify.** The new test, plus a manual run showing rows and a working filter. Record a screenshot.

---

### t15-tui-job-detail-apply

**Objective:** A job detail view with a working **Apply** action. This is the TUI's reason to exist.

**Files:**
- Create: `src/applyuminati/tui/screens/job_detail.py`
- Modify: `src/applyuminati/tui/screens/jobs.py` (open on row select)
- Test: `tests/test_tui_apply.py` (create)

**Step 1: Write the failing test.**

```python
async def test_apply_from_detail_creates_an_attempt(container, seeded_application) -> None:
    app = TuiApp(container)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("enter")  # open detail for the first row
        await pilot.press("a")  # apply
        await pilot.pause()
    async with container.read_repositories() as repos:
        tasks, _ = await repos.tasks.list()
        assert any(t.kind == APPLICATION_ATTEMPT_KIND for t in tasks)
```

**Step 2: Verify failure.** Expected: FAIL, no attempt task.

**Step 3: Implement.** Show the score dimensions, missing requirements, provenance, and description. Bind `a` to `action_apply`, which calls the service in a worker and then pushes the attempt id to a status line:

```python
    @work(exclusive=True, exit_on_error=False)
    async def apply_to_job(self, job_id: str) -> None:
        from applyuminati.services.attempt_service import AttemptService

        async with self.app.container.repositories() as repos:
            attempt = await AttemptService(repos).start_for_job(
                job_id=job_id, profile=None, mode=self.app.container.settings.execution_mode
            )
        self.app.query_one("#status", Static).update(
            f"Started attempt {attempt.id} — watch the Needs You screen for handoffs."
        )
```

Surface failures honestly: on `ConflictError` say an application is already in flight; on `ConfigurationError` say to import a profile first. Do not swallow them.

Since `t10` makes `autonomous_submit` the default, show the mode on this screen so the user can see what pressing Apply will do.

**Step 4: Verify.** The new test, then a real run against a discovered job. Record the resulting attempt state in §8.

---

### t16-tui-needs-you-screen

**Objective:** Resolve human-handoffs from the terminal — the screen that makes `autonomous_submit` safe to ship.

**Files:**
- Create: `src/applyuminati/tui/screens/needs_you.py`
- Test: `tests/test_tui_needs_you.py` (create)

**Step 1: Write the failing test.** Seed a waiting attempt, open the screen, resolve it with `done_continue`, assert the intervention closes and a resume task is queued.

**Step 2: Verify failure.** Expected: FAIL.

**Step 3: Implement.** Mirror the WebUI's four resolutions, which are already complete at `apps/web/src/pages/NeedsYou.tsx:133-179`: `answer`, `done_continue`, `keep_control`, `skip_application`. Call the same service the API route calls (`api/routers/inbox.py:119`), not a parallel implementation.

Show the attempt's reason, message, and host presence — `host_presence` is already computed at `api/routers/inbox.py:64` and is what tells a user whether a paired Browser Host is actually reachable. Add an `open_browser` action equivalent to `inbox.py:93`.

Poll on an interval with a worker, or refresh on screen activation. Whichever is chosen, use `exit_on_error=False`.

**Step 4: Verify.** The new test, then a real run showing a waiting attempt and a successful resolve. Record the observed before/after workflow state in §8.

---

### t17-tui-dashboard-screen

**Objective:** The `status` command's information as a screen.

**Files:**
- Create: `src/applyuminati/tui/screens/dashboard.py`
- Test: `tests/test_tui_dashboard.py` (create)

**Step 1: Write the failing test.** Assert the counters render for a seeded database.

**Step 2: Verify failure.**

**Step 3: Implement.** Call `DashboardService.build()` — the same service `cli/main.py:553` and `api/routers/settings.py:23` use. Render total jobs, shortlisted, ready, submitted, needs attention, scored, by-recommendation, and the latest run. Use a grid layout rather than deep nesting, which the Textual layout guide explicitly recommends.

**Step 4: Verify.** Test plus a manual run.

---

### t18-tui-sources-settings-screen

**Objective:** Enable/disable sources, **configure their options**, and edit the search strategy.

**Files:**
- Create: `src/applyuminati/tui/screens/sources.py`
- Create: `src/applyuminati/tui/screens/settings.py`
- Test: `tests/test_tui_sources.py` (create)

**Step 1: Write the failing test.** Enable a source with `{"boards": ["example"]}` through the screen's form and assert it is stored.

**Step 2: Verify failure.**

**Step 3: Implement.** `SourceInfo` already carries `options` and `options_schema` (`api/types.ts:394,396`), so the TUI can render a form from the plugin's own JSON Schema instead of hardcoding a Greenhouse field list. Render text inputs for string properties and a comma-separated input for array-of-string, which covers the three current sources.

This is the capability the CLI cannot perform at all today — there is no `--options` flag on `sources enable` (`cli/main.py:389`). If it is cheap, add that flag in this task too so the surfaces stay aligned; otherwise record the omission in §8 as a deliberate deferral to `t25`, which will flag it.

Also render the search strategy dials and persist them via the same service `PUT /settings/strategy` uses.

**Step 4: Verify.** Test plus a manual run that a configured board list survives a restart.

---

### t19-tui-profile-screen

**Objective:** Import and inspect the career profile from the terminal.

**Files:**
- Create: `src/applyuminati/tui/screens/profile.py`
- Test: `tests/test_tui_profile.py` (create)

**Step 1: Write the failing test.** Import a JSON Resume fixture through the screen and assert the profile is stored.

**Step 2: Verify failure.**

**Step 3: Implement.** Offer a file path and a paste-a-document box, both calling `ProfileService.import_from_path` / the import service the API route uses. Show the claim ledger summary: name, headline, claim and metric counts, and the claim levels the WebUI already renders. Show a clear "no profile imported" state with the next action, rather than an empty screen.

**Step 4: Verify.** Test plus a manual import.

---

### t20-tui-headless-test-suite

**Objective:** Every TUI screen is covered by a test that runs headlessly in CI.

**Files:**
- Create: `tests/test_tui_contract.py` (one test per screen)
- Modify: `.github/workflows/ci.yml` only if a new dependency is needed

**Step 1: Write one test per screen** asserting: the screen mounts, the primary data table or content region is present, and the screen's key binding exists. A screen that renders but whose worker crashes on mount must fail — assert on content, not merely on `app.query_one` succeeding.

**Step 2: Cover the worker-start path.** Add a test that the attempt worker starts on mount and stops cleanly on unmount, because a leaked task makes the whole test suite hang at teardown.

**Step 3: Verify.** `uv run pytest tests/test_tui_*.py -v` and then the full suite. Expected: green.

**Step 4: Check for a Textual deprecation trap.** `textual.widgets.TextLog` **no longer exists** — it was renamed to `RichLog` in 0.32.0 and is absent from `__all__` in 8.2.8. If any log pane is needed, import `RichLog`. `filterwarnings = ["error"]` at `pyproject.toml:200` means any Textual deprecation warning fails the suite; run the full suite and fix what it surfaces rather than adding warning filters.

**Step 5: Do not add snapshot tests.** `pytest-textual-snapshot` baselines break on theming and selection changes that are not visually significant, and they add a dependency for little value here. Assert on structure and behaviour instead. Record this choice in §8.

---

### t21-web-missing-actions

**Objective:** Give the WebUI the discover, score, and apply actions it has hooks for but never mounts.

**Files:**
- Modify: `apps/web/src/pages/Jobs.tsx`
- Modify: `apps/web/src/pages/JobDetail.tsx`
- Modify: `apps/web/src/pages/Dashboard.tsx`
- Modify: `apps/web/src/api/hooks.ts` (add `useApplyToJob` if absent)
- Modify: `apps/web/src/api/types.ts` (add the apply DTOs)
- Test: `apps/web/src/__tests__/` (create or modify)

**Step 1: Write the failing tests.** With Vitest and Testing Library, assert a Discover button calls `useDiscover`, and an Apply button calls the apply mutation and shows the returned attempt id.

**Step 2: Verify failure.** Run `cd apps/web && npm run test`. Expected: FAIL, the buttons do not exist.

**Step 3: Implement.** The hooks already exist and are unused — `useDiscover` at `api/hooks.ts:227`, `useScore` at `:242`. Wire them to real buttons. Add `useApplyToJob`:

```ts
export function useApplyToJob(): UseMutationResult<ApplyJobResponse, ApiError, string> {
  const client = useQueryClient();
  return useMutation<ApplyJobResponse, ApiError, string>({
    mutationFn: (jobId: string) =>
      post<ApplyJobResponse>(`/jobs/${encodeURIComponent(jobId)}/apply`, {}),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.dashboard }),
        client.invalidateQueries({ queryKey: queryKeys.jobList }),
      ]);
    },
  });
}
```

Confirm the exact `post` signature and CSRF handling in `api/client.ts:205` — unsafe methods already attach the CSRF header, and the new route is a POST, so this must work without extra handling. Verify it.

Also fix `Jobs.tsx:23-25`, where the source `<select>` contains only a hardcoded "All sources" option and is never populated from `useSources`, and replace the empty-state text at line 29 that tells the user to go run the CLI.

Add a 409 handler for an in-flight application: show "already in progress" rather than a generic error.

**Step 4: Verify.** `npm run test`, `npm run typecheck`, `npm run lint`, `npm run build`. Expected: all green.

---

### t22-web-applications-page

**Objective:** An applications list with state transitions, matching the TUI.

**Files:**
- Create: `apps/web/src/pages/Applications.tsx`
- Modify: `apps/web/src/App.tsx` (add the route)
- Modify: `apps/web/src/components/Layout.tsx` (nav entry)
- Test: `apps/web/src/__tests__/` (create or modify)

**Step 1: Write the failing test.** Assert the route renders and lists applications from `useApplications`.

**Step 2: Verify failure.**

**Step 3: Implement.** The hooks `useApplications` (`:340`), `useApplication` (`:357`), and `useTransitionApplication` (`:383`) already exist unused. Build the list and a detail view. Drive transitions from the server's `allowed_transitions` on the detail response (`api/mappers.py` supplies them) rather than hardcoding a state list, so the UI cannot offer an illegal move.

**Step 4: Verify.** The full web check set.

---

### t23-web-source-options-and-prefs

**Objective:** A source options form and a profile preferences editor.

**Files:**
- Modify: `apps/web/src/pages/Settings.tsx`
- Modify: `apps/web/src/pages/Profile.tsx`
- Modify: `apps/web/src/api/hooks.ts` (add `useUpdatePreferences` if absent)
- Test: `apps/web/src/__tests__/` (create or modify)

**Step 1: Write the failing tests.** Assert the options form renders inputs from `options_schema` and submits `{ options }`; assert the preferences form submits to `PUT /profile/preferences`.

**Step 2: Verify failure.**

**Step 3: Implement.** Build the form from the plugin-supplied `options_schema` so a new source needs no WebUI change. Send options through the existing `useToggleSource`, which already accepts `options` (`api/hooks.ts:321-325`) and is currently called without them (`Settings.tsx:51`). Add `useUpdatePreferences` — no hook for that route exists today.

Keep the WebUI and TUI form-generation logic conceptually aligned so `t25` can assert parity, even though the two languages cannot share code.

**Step 4: Verify.** The full web check set.

---

### t24-web-parity-tests

**Objective:** The new WebUI actions are covered by tests.

**Files:**
- Modify: `apps/web/src/__tests__/`
- Test target: `npm run test`

**Step 1: Add tests** for each action added in `t21`–`t23`: discover, score, apply, applications list, transition, source options, preferences. Assert the mutation fires and the query cache invalidates.

**Step 2: Verify.** `npm run test` passes and the new tests fail if the corresponding button is removed. Confirm by temporarily removing one button and watching the test fail, then restoring it.

**Step 3: Verify the real app.** Start `uv run applyuminati serve` and the Vite dev server, and click through discover → score → apply → needs-you in a browser. Report what you observed. Automated tests do not prove the UI is usable; this step does.

---

### t25-parity-contract (DONE)

**Objective:** One manifest that states which capability exists on which surface, enforced by a test so the surfaces cannot drift again.

**Files:**
- Create: `src/applyuminati/surface_parity.py`
- Create: `tests/test_surface_parity.py`
- Create: `docs/parity.md` (generated view, committed for humans)

**Step 1: Write the failing test first**, against a manifest that does not exist yet.

**Step 2: Implement the manifest** as data, not as scattered assertions:

```python
# src/applyuminati/surface_parity.py
"""One source of truth for which capability exists on which surface.

The CLI, the HTTP API, the TUI, and the WebUI drifted apart because each was
built against whatever its own layer happened to expose. This table is the
contract: a capability is either present on a surface or it is not, and
tests/test_surface_parity.py fails when a surface and this file disagree.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Capability:
    id: str
    summary: str
    cli_command: str | None
    api_route: str | None
    tui_binding: str | None
    web_route: str | None


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "list-jobs",
        "Browse discovered jobs",
        "jobs list",
        "GET /api/v1/jobs",
        "tui:screens.jobs",
        "/jobs",
    ),
    Capability(
        "job-detail",
        "Inspect one job",
        None,
        "GET /api/v1/jobs/{id}",
        "tui:screens.job_detail",
        "/jobs/:id",
    ),
    Capability(
        "discover",
        "Run a discovery run",
        "jobs discover",
        "POST /api/v1/jobs/discover",
        "tui:screens.jobs",
        "/jobs",
    ),
    Capability(
        "score",
        "Score jobs against the profile",
        "jobs score",
        "POST /api/v1/jobs/score",
        "tui:screens.jobs",
        "/jobs",
    ),
    Capability(
        "apply",
        "Start an application",
        "applications apply",
        "POST /api/v1/jobs/{id}/apply",
        "tui:screens.job_detail",
        "/jobs/:id",
    ),
    Capability(
        "needs-you",
        "See human handoffs",
        None,
        "GET /api/v1/needs-you",
        "tui:screens.needs_you",
        "/needs-you",
    ),
    Capability(
        "resolve-handoff",
        "Resolve a handoff",
        None,
        "POST /api/v1/needs-you/{attempt}/{intervention}",
        "tui:screens.needs_you",
        "/needs-you",
    ),
    Capability(
        "import-profile",
        "Import a JSON Resume",
        "profile import",
        "POST /api/v1/profile/import",
        "tui:screens.profile",
        "/profile",
    ),
    Capability(
        "preferences",
        "Edit search preferences",
        None,
        "PUT /api/v1/profile/preferences",
        "tui:screens.settings",
        "/profile",
    ),
    Capability(
        "toggle-source",
        "Enable or disable a source",
        "sources enable",
        "POST /api/v1/sources/{slug}/enable",
        "tui:screens.sources",
        "/settings",
    ),
    Capability(
        "source-options",
        "Configure a source's options",
        "sources enable --options",
        "POST /api/v1/sources/{slug}/enable",
        "tui:screens.sources",
        "/settings",
    ),
    Capability(
        "strategy",
        "Edit the search strategy",
        None,
        "PUT /api/v1/settings/strategy",
        "tui:screens.settings",
        "/settings",
    ),
    Capability(
        "dashboard",
        "Pipeline overview",
        "status",
        "GET /api/v1/dashboard",
        "tui:screens.dashboard",
        "/",
    ),
    Capability(
        "capabilities",
        "Plugin maturity matrix",
        "capabilities",
        None,
        "tui:screens.dashboard",
        None,
    ),
    Capability(
        "browser-hosts",
        "Pair and revoke Browser Hosts",
        "browser-host pair",
        "POST /api/v1/browser-hosts/pair",
        None,
        None,
    ),
)
```

Populate `cli_command` honestly. Several entries are `None` because no CLI command exists — that is the point of the file. If a row is unwinnable this cycle, leave it `None` and say so in `docs/parity.md` rather than deleting the row or quietly marking it done.

**Step 3: Write the enforcement tests**, one per surface, each cheap and specific:

- **CLI**: introspect the Typer app's registered command tree and assert every non-`None` `cli_command` resolves.
- **API**: walk `create_app().routes` and assert every non-`None` `api_route` exists with the declared method.
- **TUI**: import the named screen module and assert it registers the declared binding.
- **Web**: parse `apps/web/src/App.tsx` and assert every non-`None` `web_route` is routed.

**Step 4: Generate `docs/parity.md`** from the manifest with a small script and a `make parity` target, so the human-readable table cannot drift from the machine-readable one.

**Step 5: Verify.** `uv run pytest tests/test_surface_parity.py -v`. Every assertion must pass. Where a capability is genuinely absent, the manifest records `None` and the test correctly asserts nothing — that is a truthful contract, not a passing test hiding a gap. Run the full suite and the web checks one final time.

---

## 7. Evidence and decisions log

| When | Task / environment | Expected vs observed | Outcome / evidence | Correction or next action |
|---|---|---|---|---|
| 2026-09-25, this machine | Investigation: `uv run pytest -q` | Suite green | **fail (1) / pass (426)** in 48.67s. Sole failure `tests/test_ego_lite.py::test_a_backend_without_the_helper_is_not_selectable` asserts `"macOS" in report.detail`; on this Mac the helper exists but fails its smoke marker. Environment-coupled. | Tracked as `t08`. Known baseline until then. |
| 2026-09-25 | Investigation: live Greenhouse discovery (`boards=["stripe","anthropic"]`) | Jobs fetched over the network | **pass** — `jobs_discovered=200 jobs_created=194 jobs_merged=6 state=partial`; 1.88s. Dedup and normalisation are real. | None. |
| 2026-09-25 | Investigation: live Lever discovery (`companies=["tala"]`) | Jobs fetched | **pass** — `jobs_discovered=6 jobs_created=6 state=succeeded`. A retired tenant (`plaid`) 404s and the plugin misreports it as `source endpoint is gone`; wording issue only, not a blocker. | Note the misleading 404 detail for a future polish task. |
| 2026-09-25 | Investigation: `init` then `doctor` | Working first run | **fail** — `OperationalError: no such table: profiles`. `init` also reported creating `config.toml` that did not exist on disk. | Tracked as `t05`. |
| 2026-09-25 | Investigation: `GET /` and all `/api/v1/*` with no password | UI served, API reachable | **fail** — every route 503 `auth.not_configured`; `/` 404 because `web_dist` defaults to `None`. With `APPLYUMINATI_SECURITY__PASSWORD` and `web_dist` set: SPA 200 and all 7 endpoints 200. | Tracked as `t05` and `t06`. |
| 2026-09-25 | Investigation: `config.toml` source options | Config applied on startup | **fail** — discovery returned 0 jobs in 0.03s. `SourceService.sync_from_settings` (`services/source_service.py:145`) has zero callers repo-wide. No CLI flag supplies options. | Tracked as `t04`. |
| 2026-09-25 | Investigation: JSON Resume import | Spec-valid file imports | **fail** — `basics.location: "Austin, TX"` rejected; `core/models/jsonresume.py:50` types it as an object only. | Tracked as `t07`. |
| 2026-09-25 | Investigation: apply engine driven manually past the missing entry point | Attempt advances | **pass** — selected `playwright`, logged `attempt.local_session_opened`, reached `workflow_state=waiting_for_human` on a real Lever job. The automation is real. | Confirms the engine works; the blockers are entry point and persistence only. |
| 2026-09-25 | Investigation: `TaskWorker.run_once` on a clean data dir | Attempt persists | **fail (blocking)** — `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked` on `UPDATE application_attempts SET browser_backend=?`. Reproduced 3x, including inside the real API server's own lifespan worker. Delay from `browser.selected` to the error was 10.4s, matching `busy_timeout=10000` at `db/session.py:41`. | Tracked as `t01`. Root cause: `claim_next` (`db/repositories/tasks.py:46-81`) flushes but never commits, so the write lock is held while the handler opens a second session (`attempt_tasks.py:243`, `repos=None`). |
| 2026-09-25 | Investigation: reachability of the apply path | A user can start an application | **fail (blocking)** — `AttemptService.create` has zero callers in `src/`; no `POST /api/v1/applications`; no `apply` CLI command; no UI button. `POST /api/v1/browser-hosts/pair` is the only creating route in the API. | Tracked as `t02`. |
| 2026-09-25 | Static audit: attempt-to-application link | Submission state propagates | **fail** — `attempt_tasks` never touches `repos.applications`, so a confirmed submission never moves the `Application` to `SUBMITTED`. | Tracked as `t03`. |
| 2026-09-25 | Static audit: WebUI actions | Surfaces match the API | **fail** — `useDiscover`, `useScore`, `useApplications`, `useApplication`, `useTransitionApplication` all exist unused in `api/hooks.ts`; no discover, score, or apply button; no applications route; `Jobs.tsx:23-25` source select is inert. | Tracked as `t21`–`t24`. |
| 2026-09-25 | Decision | — | User chose: TUI calls services **in-process**; distribution adds **PyPI**; default execution mode becomes **`autonomous_submit`**. | TUI must never call `asyncio.run` and must start the attempt worker itself. `t10` changes one default and the docs; the fabrication guard, anti-evasion rules, and idempotency fingerprint are untouched. Risk was stated before the choice and accepted. |
| 2026-09-25 | Research: Textual version and API | A current, citable API | **pass** — stable is **8.2.8** (2026-06-30), `Development Status :: 5 - Production/Stable`, strict semver; requires `rich>=14.2.0`; `TextLog` was renamed `RichLog` in 0.32.0 and does not exist in 8.2.8. `App.run()` touches the global event loop, so it conflicts with `asyncio.run`. `DataTable` defaults to `cursor_type="cell"` and `RowSelected` requires `"row"`. | Pin `textual>=8.2,<9`; bump `rich` to `>=14.2`; use `@work` and `RichLog`; construct tables with `cursor_type="row"`. |
| 2026-09-25 | Research: project config | Ready for TUI tests and packaging | **pass** — `asyncio_mode = "auto"` (`pyproject.toml:193`) and `pytest-asyncio>=0.25` are already configured, so `App.run_test()` works with no config change. `filterwarnings = ["error"]` will surface Textual deprecations as failures. Hatchling packages `src/applyuminati` by default, so `.tcss` files should be included — confirm in `t11`. | No test-config change needed. Verify `.tcss` packaging rather than assuming. |

| 2026-09-26 | `t01`: new `tests/test_attempt_worker_persistence.py`, unfixed source | Fails with `database is locked` | **pass (expected failure)** — `1 failed in 10.67s`; `worker.task_crashed` on `UPDATE application_attempts SET browser_backend=?`; attempt left `pending`. The 10.67s matches `busy_timeout=10000` at `db/session.py:41`. | Confirms the test reproduces the production bug rather than an incidental error. |
| 2026-09-26 | `t01`: fix applied, same test | Passes | **pass** — `1 passed in 0.23s`. 10.67s to 0.23s. Fix is two `await self._session.commit()` calls, in `claim_next` and `reclaim_expired_leases`. | None. |
| 2026-09-26 | `t01`: full suite | `1 failed, 427 passed` | **pass** — `1 failed, 427 passed in 43.78s`. Sole failure `tests/test_ego_lite.py:334`, pre-existing and environment-coupled, tracked as `t08`. | New baseline for every later task. |
| 2026-09-26 | `t01`: gates | All clean | **pass** — `ruff format --check` 196 files already formatted, `ruff check` all checks passed, `lint-imports` 4 contracts kept, `pyright` 0 errors. | `ruff format .` also reformats Python blocks inside `.md`, so the plan file was rewritten by the format pass. Re-validated the frontmatter and diagrams afterwards. |
| 2026-09-26 | `t01`: end-to-end smoke, real Lever tenant `tala` | Attempt persists through the worker | **pass** — discovery 6 jobs, scored 6, attempt created, task submitted, `TaskWorker.run_once` returned `did_work=True`, log shows `attempt.browser_selected backend=playwright`, `attempt.local_session_opened`, `attempt.step_finished workflow_state=waiting_for_human`, `worker.task_succeeded duration_s=1.42`. Persisted: `browser_backend=playwright`, `browser_session_id=01M3E6HWC7Q56SMWG0EJ3X40AW`, `intervention[ambiguous_question] Question needs an answer: Full name`. | Was `worker.task_crashed` before the fix. The engine now reaches a real ATS form question and pauses. Throwaway scripts deleted. |
| 2026-09-26 | Correction to `t01` plan text | — | The plan's `t01` test sketch passed a shared `repos`, which every existing execution test also does. That shape cannot reproduce the bug, because the handler then shares the caller's session. The real test must go through `TaskWorker.run_once` so the handler opens its own session. | Rewrote the test to the worker path. Also had to bind `set_container(ServiceContainer(settings, database=database))` to the fixture database, because the handler resolves `get_container()` and a stale singleton from an earlier test pointed elsewhere: the test passed alone and failed in the full suite until that was fixed. |

---

## 8. Tests and validation

### Per-task commands

```bash
# Python
uv run pytest tests/<file>.py -v     # one task
uv run pytest -q                     # full suite; baseline 1 known failure until t08
uv run ruff check .
uv run pyright
uv run lint-imports                  # import-linter contracts

# WebUI
cd apps/web && npm run test && npm run typecheck && npm run lint && npm run build

# Packaging
uv build && uv run python -m twine check dist/*
```

### Acceptance criteria

| ID | Criterion | Verified by |
|---|---|---|
| AC-1 | An attempt persists `browser_backend` and leaves `pending` when run through `TaskWorker.run_once`, with no `database is locked` | `t01` failing-then-passing test on a file-backed SQLite db |
| AC-2 | A user can start an application from the API, the CLI, and both UIs | `t02` service/API/CLI tests, `t15` TUI test, `t21` WebUI test |
| AC-3 | A clean install reaches a working UI with one command | `t05` test plus a real `init` → `doctor` run |
| AC-4 | The package installs from PyPI and the CLI runs | `t11` `uv build`, `twine check`, clean-venv install |
| AC-5 | `applyuminati tui` starts with no server running | `t13` headless test plus a manual screenshot |
| AC-6 | Every TUI screen is covered headlessly | `t20` one test per screen |
| AC-7 | CLI, API, TUI, and WebUI match one capability manifest | `t25` one enforcement test per surface |

### Manual verification that tests cannot replace

Per the workflow rules, run the real thing. At minimum: launch the TUI and screenshot each screen; click through discover → score → apply → needs-you in a browser; run `init` on a clean data dir; and install the built wheel into a throwaway venv. Record each observation in §7.

---

## 9. Risks, tradeoffs, and open questions

**Autonomous submit as the default is the largest risk in this plan.** I raised it before the choice and the user overrode me, so it proceeds. The concrete consequences: the README's safety claim and `SECURITY.md` must change (`t12`); a first-time user who presses Apply submits a real application without an extra confirmation step; support burden rises; and the fabrication guard, CAPTCHA detection, and idempotency fingerprint become load-bearing rather than belt-and-braces. Mitigation chosen: change only the default, keep every guard intact, and make the Needs You screen prominent (`t16`) so the handoff point stays visible. If submissions go wrong in practice, `t10` is a one-line revert.

**The `t01` fix changes transaction semantics.** Committing the claim means a crash after claiming leaves the task `RUNNING` with a lease, reclaimed by `reclaim_expired_leases` on the next poll. That is the intended design and the reason a lease exists, but it is a real behaviour change: previously the claim vanished with the transaction. The `t01` test must assert the lease is durable, not just that the lock is gone.

**SQLite is the only database exercised.** Everything was verified on SQLite. PostgreSQL is a README "Planned" item, so the queue is not tested there. The nested-session pattern fixed in `t01` is a portability risk if Postgres arrives: the fix removes the SQLite lock contention but does not by itself prove correct isolation semantics on a server database. Record this when Postgres work starts.

**The TUI duplicates presentation logic the WebUI also lacks.** Three surfaces means three places for a capability to be missing — which is why `t25` exists. The manifest keeps the surfaces honest, but it cannot make the UIs feel identical; only the capability set is guaranteed.

**`filterwarnings = ["error"]` will surface Textual deprecations.** Expected, and the fix is to update the code, not to add ignore filters. If a specific third-party warning proves unavoidable, record it in §7 with the reason rather than silencing it silently.

**Open questions.** The PyPI project name and owner must be confirmed, and a Trusted Publisher configured, before the first publish. Whether the CLI should gain `sources enable --options` in `t18` or defer to `t25` is a judgement call; if deferred, `t25` will correctly report the gap, which is the honest outcome.

**Unverified assumptions to confirm during execution.** That `AttemptRepository.active_for_job` and `ConflictError` do not already exist under another name; that `JobService.list` returns the shape `t14` assumes; that `ResumeLocation` has a field for a free-text location in `t07`; and that Hatchling ships `.tcss` files inside the wheel in `t11`. Each is marked "check and add if absent" in its task rather than assumed.
