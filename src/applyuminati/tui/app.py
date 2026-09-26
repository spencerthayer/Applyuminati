"""The root Textual application.

Two constraints shape this module and are not negotiable:

* **Textual owns the event loop.** ``App.run`` drives the global loop, so the
  CLI helper ``_run_async`` (which is ``asyncio.run``) must never be used
  anywhere in this package. Every service call is a Textual worker, which runs
  on the app's loop with no thread hop.
* **The attempt worker is started here.** The API starts it in its lifespan; a
  TUI has no lifespan, so without this the apply engine would never run and the
  UI would sit idle.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

if TYPE_CHECKING:
    from applyuminati.services.container import ServiceContainer
    from applyuminati.tui.screens.jobs import JobSelected

#: Polled by the attempt worker. A second is the same cadence the API uses.
_WORKER_POLL_SECONDS = 1.0


class TuiApp(App[None]):
    """Root application. Concrete screens are pushed on top of this."""

    # Do not autofocus a text field. A focused Input consumes every printable
    # key, which silently kills the nav bindings the moment the jobs screen
    # mounts. Fields are reached deliberately instead.
    AUTO_FOCUS = None

    TITLE = "Applyuminati"
    SUB_TITLE = "local-first job search"
    CSS_PATH = "styles.tcss"

    # Annotated as the base class declares it, not as a plain list: pyright
    # rejects widening a base-class symbol.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        # Digits, not letters. A focused Input consumes printable keys, so a
        # single-letter nav would be swallowed by the jobs search box and would
        # also hijack half of what a user types into it. priority=True is not a
        # reliable escape: binding delivery still depends on what has focus.
        Binding("1", "go('dashboard')", "Dashboard", priority=True),
        Binding("2", "go('jobs')", "Jobs", priority=True),
        Binding("3", "go('needs_you')", "Needs you", priority=True),
        Binding("4", "go('sources')", "Sources", priority=True),
        Binding("5", "go('settings')", "Settings", priority=True),
        Binding("6", "go('profile')", "Profile", priority=True),
        Binding("slash", "focus_search", "Search", priority=True),
    ]

    #: Screen name to the class that implements it. Resolved lazily so a missing
    #: optional screen cannot stop the app from starting.
    SCREEN_PATHS: ClassVar[dict[str, str]] = {
        "dashboard": "applyuminati.tui.screens.dashboard:DashboardScreen",
        "jobs": "applyuminati.tui.screens.jobs:JobsScreen",
        "needs_you": "applyuminati.tui.screens.needs_you:NeedsYouScreen",
        "sources": "applyuminati.tui.screens.sources:SourcesScreen",
        "settings": "applyuminati.tui.screens.settings:SettingsScreen",
        "profile": "applyuminati.tui.screens.profile:ProfileScreen",
    }

    def __init__(self, container: ServiceContainer | None = None) -> None:
        super().__init__()
        self._container = container
        self._worker_stop: asyncio.Event | None = None
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def container(self) -> ServiceContainer:
        """The process container, built on first use."""
        if self._container is None:
            from applyuminati.services.container import get_container

            self._container = get_container()
        return self._container

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="content"):
            yield Static("Starting Applyuminati...", id="placeholder")
        yield Footer()

    def _resolve_screen(self, name: str) -> type[Any] | None:
        target = self.SCREEN_PATHS.get(name)
        if target is None:
            return None
        from importlib import import_module

        module_path, _, class_name = target.partition(":")
        try:
            return getattr(import_module(module_path), class_name)
        except (ImportError, AttributeError):
            return None

    async def action_go(self, name: str) -> None:
        """Swap to a top-level screen by name."""
        screen = self._resolve_screen(name)
        if screen is None:
            self.notify(f"No screen named {name!r}", severity="warning")
            return
        if isinstance(self.screen, screen):
            return
        # Replace the top-level screen rather than growing a stack the user has
        # to pop out of. Written as pop-then-push rather than switch_screen
        # because Textual 8.2.8 raises IndexError in _pop_result_callback when
        # switching away from a screen that was itself switched in without a
        # result callback, which is exactly this shape.
        if len(self.screen_stack) > 1:
            self.pop_screen()
        await self.push_screen(screen())

    def action_focus_search(self) -> None:
        """Focus the search field if the current screen has one."""
        try:
            self.screen.query_one("#search").focus()
        except Exception:
            return

    def on_job_selected(self, event: JobSelected) -> None:
        """A row in the jobs list was activated; open its detail."""
        from applyuminati.tui.screens.job_detail import JobDetailScreen

        self.push_screen(JobDetailScreen(job_id=event.job_id))

    async def on_mount(self) -> None:
        from applyuminati.core.settings import get_settings
        from applyuminati.services.attempt_tasks import (
            register_attempt_handlers,
            run_attempt_worker_forever,
        )

        settings = get_settings()
        settings.ensure_directories()
        register_attempt_handlers()
        # A config file is how a pip install configures sources, and the CLI
        # honours it, so the TUI must too.
        with contextlib.suppress(Exception):
            await self.container.sync_settings_to_db()

        self._worker_stop = asyncio.Event()
        self._worker_task = asyncio.create_task(
            run_attempt_worker_forever(
                poll_interval=_WORKER_POLL_SECONDS,
                stop_event=self._worker_stop,
            ),
            name="tui-attempt-worker",
        )
        await self.action_go("dashboard")

    async def _flush_worker_start(self) -> None:
        """Let the worker task reach its first await. Used by tests."""
        for _ in range(3):
            await asyncio.sleep(0)

    async def on_unmount(self) -> None:
        if self._worker_stop is not None:
            self._worker_stop.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        # The container is deliberately not closed. A TUI is the whole process,
        # so the SQLite engine's worker threads die with it, and a container
        # handed in by a caller (a test, an embedder) is that caller's to close.
