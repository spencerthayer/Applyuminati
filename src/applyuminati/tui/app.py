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
from typing import TYPE_CHECKING, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

if TYPE_CHECKING:
    from applyuminati.services.container import ServiceContainer

#: Polled by the attempt worker. A second is the same cadence the API uses.
_WORKER_POLL_SECONDS = 1.0


class TuiApp(App[None]):
    """Root application. Concrete screens are pushed on top of this."""

    TITLE = "Applyuminati"
    SUB_TITLE = "local-first job search"
    CSS_PATH = "styles.tcss"

    # Annotated as the base class declares it, not as a plain list: pyright
    # rejects widening a base-class symbol.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

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
        self.query_one("#placeholder", Static).update(
            "Ready. Press ctrl+q to quit, or run `applyuminati --help` for the CLI."
        )

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
