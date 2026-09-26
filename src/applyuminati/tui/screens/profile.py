"""The profile screen.

Import the canonical career profile and show what was derived from it. The
WebUI offers two ways in — a pasted document and a file — and so does this:
:class:`~applyuminati.services.profile_service.ProfileService` has a method for
each, and neither is a strict subset of the other, because only the path
reader can say *which file* it could not read.

Two rules from :mod:`applyuminati.tui.app` apply here: Textual owns the event
loop, so every service call is a ``@work`` worker rather than ``asyncio.run``,
and the container is reached through ``self.app.container``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Static, TextArea

from applyuminati.core.errors import NotFoundError
from applyuminati.services.profile_service import ProfileService
from applyuminati.services.views import ImportResult, ProfileView

if TYPE_CHECKING:
    from applyuminati.services.container import ServiceContainer
    from applyuminati.tui.app import TuiApp

#: The service call an import makes, given an already-wired service.
_Import = Callable[[ProfileService], Awaitable[ImportResult]]

#: What the screen says when there is nothing to show. This is the first thing
#: a new user sees, so it names the action instead of describing the absence.
_EMPTY_STATE = (
    "No career profile yet.\n"
    "Next step: paste a JSON Resume document into the box below, "
    "or type the path to a resume.json file and press enter.\n"
    "Nothing can be scored or applied to until a profile is imported."
)


class ProfileScreen(Screen[None]):
    """Import a career profile, then show its identity and claim ledger."""

    #: Owned here rather than in the app stylesheet: this screen is the only
    #: thing that knows how its own two-column form should be laid out.
    DEFAULT_CSS = """
    ProfileScreen {
        layout: vertical;
    }

    #profile-scroll {
        height: 1fr;
    }

    #profile-import {
        height: auto;
        border: round $accent;
        padding: 0 1;
    }

    #profile-path-pane {
        width: 2fr;
        height: auto;
        padding: 0 1 0 0;
    }

    #profile-document-pane {
        width: 3fr;
        height: auto;
    }

    #profile-document {
        height: 8;
    }

    #profile-status, #profile-error {
        height: auto;
        color: $text-muted;
    }

    #profile-error {
        color: $error;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="profile-scroll"):
            yield Static(_EMPTY_STATE, id="profile-summary")
            yield Static("", id="profile-claim-levels")
            with Horizontal(id="profile-import"):
                with Vertical(id="profile-path-pane"):
                    yield Label("Resume file")
                    yield Input(placeholder="/path/to/resume.json", id="profile-path")
                    yield Button("Import from path", id="import-path")
                with Vertical(id="profile-document-pane"):
                    yield Label("Or paste a JSON Resume document")
                    yield TextArea(id="profile-document")
                    yield Button("Import pasted document", id="import-document")
            yield Static("", id="profile-error")
            yield Static("", id="profile-status")

    @property
    def _container(self) -> ServiceContainer:
        """The process container. ``TuiApp`` is the app that provides it."""
        return cast("TuiApp", self.app).container

    # -- rendering --------------------------------------------------------

    def _show_empty(self) -> None:
        self.query_one("#profile-summary", Static).update(_EMPTY_STATE)
        self.query_one("#profile-claim-levels", Static).update("")

    def _show_profile(self, view: ProfileView) -> None:
        lines = [
            view.name or "Unnamed",
            " · ".join(part for part in (view.headline, view.email) if part),
            f"label: {view.label}",
            f"claims: {view.counts['claims']}",
            f"metrics: {view.counts['metrics']}",
        ]
        self.query_one("#profile-summary", Static).update("\n".join(line for line in lines if line))
        levels = "".join(
            f"{level}: {count}\n" for level, count in sorted(view.claim_levels.items())
        )
        self.query_one("#profile-claim-levels", Static).update(
            f"\nclaim levels\n{levels}" if levels else ""
        )

    def _set_status(self, message: str) -> None:
        self.query_one("#profile-status", Static).update(message)

    def _set_error(self, message: str) -> None:
        self.query_one("#profile-error", Static).update(message)

    # -- loading ----------------------------------------------------------

    def on_mount(self) -> None:
        self._reload()

    @work(exclusive=True, exit_on_error=False)
    async def _reload(self) -> None:
        try:
            async with self._container.read_repositories() as repos:
                view = await ProfileService(repos).view()
        except NotFoundError:
            self._show_empty()
            return
        except Exception as exc:  # a failed read is the user's to see, not ours to swallow
            self._set_error(f"Could not read the profile: {exc}")
            return
        self._set_error("")
        self._show_profile(view)

    # -- importing --------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "profile-path":
            self._import_from_path()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "import-path":
            self._import_from_path()
        elif event.button.id == "import-document":
            self._import_document()

    def _import_from_path(self) -> None:
        raw = self.query_one("#profile-path", Input).value.strip()
        if not raw:
            self._set_error("Type the path to a JSON Resume file, or paste one instead.")
            return
        self._set_error("")
        self._import_path(Path(raw))

    def _import_document(self) -> None:
        document = self.query_one("#profile-document", TextArea).text
        if not document.strip():
            self._set_error("Paste a JSON Resume document, or type a path instead.")
            return
        self._set_error("")
        self._import_pasted(document)

    @work(exclusive=True, exit_on_error=False)
    async def _import_path(self, path: Path) -> None:
        self._set_status("Importing…")
        await self._do_import(lambda service: service.import_from_path(path, replace=True))

    @work(exclusive=True, exit_on_error=False)
    async def _import_pasted(self, document: str) -> None:
        try:
            payload = json.loads(document)
        except json.JSONDecodeError as exc:
            self._reject(f"That is not valid JSON: {exc}")
            return
        if not isinstance(payload, dict):
            self._reject("A JSON Resume must be a JSON object, not a list or a bare value.")
            return
        self._set_status("Importing…")
        await self._do_import(
            lambda service: service.import_resume(payload, replace=True, origin="tui.paste")
        )

    def _reject(self, message: str) -> None:
        """Report a document that never reached the service."""
        self._set_error(message)
        self._set_status("Nothing was imported.")

    async def _do_import(self, call: _Import) -> None:
        """Run one import on the app's loop. Never raises: it reports."""
        try:
            async with self._container.repositories() as repos:
                result = await call(ProfileService(repos))
        except Exception as exc:  # a failed import must be readable, never swallowed
            self._set_error(f"Import failed: {exc}")
            return
        self._set_error("")
        self._show_profile(result.profile)
        message = f"Imported {result.claims_created} claims and {result.metrics_extracted} metrics."
        if result.warnings:
            message += f" {len(result.warnings)} warning(s): {'; '.join(result.warnings)}"
        self._set_status(message)


__all__ = ["ProfileScreen"]
