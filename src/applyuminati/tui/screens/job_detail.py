"""The Job Detail screen: one job, in full, and the button that applies to it.

Apply is the point of the product, so this screen is built around it. Three
rules shape the module:

* **Textual owns the event loop.** ``action_apply`` never awaits the service;
  it hands off to a worker, because a database round trip inside a key handler
  would freeze the whole UI.
* **Failures are named.** A refused duplicate, a missing profile and a job
  that has gone away are three different things a user can act on, so each one
  says which it is. Nothing is flattened into "error".
* **The execution mode is on screen.** The default submits without asking, so
  the user must be able to see what pressing Apply will do before they press it.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, cast

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from applyuminati.core.errors import (
    ApplyuminatiError,
    ConfigurationError,
    DuplicateActionError,
    NotFoundError,
)
from applyuminati.core.models.job import Job
from applyuminati.core.models.scoring import FitScore
from applyuminati.core.settings import ExecutionMode
from applyuminati.services.container import ServiceContainer


class _HasContainer(Protocol):
    """What a screen needs from whatever app it was pushed onto.

    Declared as a protocol rather than naming ``TuiApp`` so the screen can be
    mounted by a test host, and so the dependency it actually has is written
    down: a container, nothing else.
    """

    @property
    def container(self) -> ServiceContainer: ...


def _container(app: object) -> ServiceContainer:
    return cast("_HasContainer", app).container


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _locations(job: Job) -> str:
    stated = [location.display() for location in job.locations]
    # ``display`` falls back to "Unspecified"; several of those is noise, and
    # one of them is honest, so collapse them.
    unique = [text for text in dict.fromkeys(stated) if text and text != "Unspecified"]
    return ", ".join(unique) if unique else "Location not stated"


def render_detail(job: Job, score: FitScore | None, mode: ExecutionMode) -> str:
    """The whole detail pane as plain text.

    A string rather than a widget tree: this is content the user reads, and it
    is the same content the tests assert on.
    """
    posted = job.posted_at.date().isoformat() if job.posted_at else "unknown"
    lines = [
        f"{job.title} — {job.company}",
        f"{_locations(job)} · {job.remote_mode.value} · {job.employment_type.value}",
        f"Apply: {job.apply_url or job.canonical_url}",
        f"Posted: {posted}",
        "",
        f"Apply mode: {mode.value}",
    ]
    if score is None:
        lines += ["", "Fit: not scored yet — no fit score exists for this job."]
    else:
        lines += [
            "",
            f"Fit: {_pct(score.overall)} · recommendation: {score.recommendation.value}",
        ]
        lines += [
            f"  {dimension.dimension.value:<24} {_pct(dimension.score)} "
            f"x {dimension.weight:.2f}  {dimension.rationale}".rstrip()
            for dimension in score.dimensions
        ]
        if score.missing_requirements:
            lines += ["", "Missing requirements"]
            lines += [
                f"  [{item.severity.value}] {item.requirement}"
                + (f" — {item.note}" if item.note else "")
                for item in score.missing_requirements
            ]
        else:
            lines += ["", "Missing requirements: none recorded"]
    description = job.description or "No description was extracted."
    lines += ["", "Description"]
    lines += [f"  {line}" for line in description.splitlines() or [description]]
    return "\n".join(lines)


class JobDetailScreen(Screen[None]):
    """Everything about one job, plus the Apply action."""

    TITLE = "Job Detail"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "apply", "Apply"),
    ]

    #: The app stylesheet docks ``#status`` to the bottom, where the Footer
    #: already sits. One row of margin puts the status line just above the
    #: Footer instead of underneath it.
    CSS = """
    #status {
        margin-bottom: 1;
    }
    """

    def __init__(self, *, job_id: str) -> None:
        super().__init__()
        self._job_id = job_id

    def compose(self) -> ComposeResult:
        # A pushed Screen gets no chrome from the app, and this screen's whole
        # job is one bare-letter key, so the binding has to be on screen.
        yield Header()
        with VerticalScroll(id="detail-pane"):
            yield Static("", id="detail-body")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._load()

    # -- content ----------------------------------------------------------
    def _set_body(self, text: str) -> None:
        self.query_one("#detail-body", Static).update(text)

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    @work(group="load", exclusive=True, exit_on_error=False)
    async def _load(self) -> None:
        container = _container(self.app)
        async with container.read_repositories() as repos:
            job = await repos.jobs.get(self._job_id)
            profile = await repos.profiles.get_active()
            score = (
                await repos.scores.latest_for(job.id, profile.id)
                if job is not None and profile is not None
                else None
            )
        if job is None:
            self._set_body(
                f"Job {self._job_id} not found.\n\n"
                "It was never recorded, or it was merged into another posting "
                "and this id no longer resolves."
            )
            return
        self._set_body(render_detail(job, score, container.settings.execution_mode))

    # -- apply ------------------------------------------------------------
    def action_apply(self) -> None:
        """Queue an application attempt. Returns immediately; the worker runs."""
        self._set_status("Starting application…")
        self._apply()

    @work(exclusive=True, exit_on_error=False)
    async def _apply(self) -> None:
        from applyuminati.services.attempt_service import AttemptService

        container = _container(self.app)
        try:
            async with container.repositories() as repos:
                attempt = await AttemptService(repos).start_for_job(
                    job_id=self._job_id,
                    profile=None,
                    mode=container.settings.execution_mode,
                )
        except DuplicateActionError as exc:
            self._set_status(f"Not applied: {exc.message}")
        except ConfigurationError as exc:
            self._set_status(f"Cannot apply: {exc.message}")
        except NotFoundError as exc:
            self._set_status(f"Cannot apply: {exc.message}")
        except ApplyuminatiError as exc:
            # Deliberately not a generic "error": anything else the service
            # refuses on purpose still has a message worth reading.
            self._set_status(f"Apply failed [{exc.code}]: {exc.message}")
        else:
            # One line: the status bar is a single row tall, and a message the
            # user cannot finish reading is worse than a short one.
            self._set_status(
                f"Started attempt {attempt.id} ({attempt.submission_mode.value}). "
                "Watch the Needs You screen."
            )


__all__ = ["JobDetailScreen", "render_detail"]
