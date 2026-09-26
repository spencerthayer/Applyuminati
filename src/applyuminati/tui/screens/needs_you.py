"""The Needs You screen: open human interventions, and how to clear them.

This is the screen that makes autonomous submission safe to ship. Every other
screen shows progress; this one shows the places where the engine deliberately
stopped and handed the browser to a person, and it offers exactly the
resolutions :class:`~applyuminati.services.attempt_service.AttemptService`
accepts. None of the logic is reimplemented here: the list is
:meth:`AttemptService.inbox`, each action is :meth:`AttemptService.resolve`,
and the browser handoff is :meth:`AttemptService.activate_browser` — the same
three calls the HTTP inbox router makes.

Two details are load-bearing.

**Host presence is shown, not inferred.** ``host_presence`` reconciles the host
id an intervention recorded with what is actually connected right now. Resolving
a handoff against a missing host does not resume: it records a reclaim failure
and leaves the pause open. A row that hides that turns a clear message ("the Mac
Browser Host is offline") into a button that appears broken.

**Every service call is a Textual worker.** ``App.run`` owns the loop, so
nothing here may start one; and each of these workers re-reads on a key press,
so a raised exception must not take the whole app down with it. Hence
``exit_on_error=False`` throughout.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from applyuminati.core.models.execution import InterventionResolution
from applyuminati.services.attempt_service import (
    AttemptService,
    HostPresence,
    InboxItem,
    host_presence,
)

if TYPE_CHECKING:
    from applyuminati.tui.app import TuiApp

_COLUMNS = ("Company", "Reason", "Instruction", "Opened", "Host")

#: Mirrors the WebUI's ``hostPresenceLabel``. Shown verbatim rather than as a
#: bool, because the four states call for different user actions and a single
#: "needs browser" flag collapses the one that matters.
_PRESENCE_LABELS: dict[HostPresence, str] = {
    HostPresence.CONNECTED: "Mac connected",
    HostPresence.OFFLINE: "Mac offline",
    HostPresence.SESSION_UNAVAILABLE: "session unavailable",
    HostPresence.NOT_REQUIRED: "in-app",
}

_OPENED_FORMAT = "%Y-%m-%d %H:%M"


class NeedsYouScreen(Screen[None]):
    """Open interventions, and the four ways to resolve each one."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("d", "resolve_done", "Done, continue", show=True),
        Binding("k", "resolve_keep", "Keep control", show=True),
        Binding("s", "resolve_skip", "Skip application", show=True),
        Binding("a", "focus_answer", "Answer", show=True),
        Binding("o", "open_browser", "Open browser", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        #: The inbox entry under the cursor, as the service handed it over. Held
        #: rather than re-read from the table so an action never resolves
        #: against a stale or half-removed row.
        self._current: InboxItem | None = None
        #: Row key (the intervention id) to inbox entry, rebuilt with the table.
        self._by_intervention_id: dict[str, InboxItem] = {}

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="content"):
            yield DataTable(id="inbox-table", cursor_type="row")
            yield Input(placeholder="Answer the question, then press enter", id="answer-input")
            yield Static("", id="empty-state")
        yield Static("", id="status")

    async def on_mount(self) -> None:
        table = self.query_one("#inbox-table", DataTable)
        table.add_columns(*_COLUMNS)
        self.query_one("#empty-state", Static).display = False
        self._load()

    def on_screen_resume(self) -> None:
        """Re-read on activation.

        The inbox is shared state: the attempt worker can open a pause, and a
        resolution elsewhere can close one, while this screen is not the active
        screen. Refreshing on entry is what keeps the list honest.
        """
        self._load()

    # -- loading ----------------------------------------------------------

    @work(group="load", exclusive=True, exit_on_error=False)
    async def _load(self) -> None:
        """Rebuild the table from the service, host presence included."""
        app = cast("TuiApp", self.app)
        async with app.container.read_repositories() as repos:
            items = await AttemptService(repos).inbox()
        manager = app.container.browser_hosts
        self._populate(
            [(item, host_presence(item.attempt, item.intervention, manager)) for item in items]
        )

    def _populate(self, entries: list[tuple[InboxItem, HostPresence]]) -> None:
        table = self.query_one("#inbox-table", DataTable)
        table.clear()
        for item, presence in entries:
            intervention = item.intervention
            table.add_row(
                item.company or "Unknown company",
                intervention.reason.value,
                intervention.instruction,
                _opened_label(intervention.opened_at),
                _PRESENCE_LABELS[presence],
                key=intervention.id,
            )
        self._by_intervention_id = {item.intervention.id: item for item, _presence in entries}
        # clear() resets the cursor, so the highlighted item is the first one
        # again rather than whatever it was before the rebuild.
        self._current = entries[0][0] if entries else None

        empty = self.query_one("#empty-state", Static)
        empty.display = not entries
        if not entries:
            empty.update("Nothing needs you. Nothing is waiting on a human right now.")

        waiting = len(entries)
        noun = "intervention" if waiting == 1 else "interventions"
        self._set_status(f"{waiting} {noun} waiting. Automation is paused here on purpose.")

    # -- actions ----------------------------------------------------------

    def action_resolve_done(self) -> None:
        self._resolve_current(InterventionResolution.DONE_CONTINUE)

    def action_resolve_keep(self) -> None:
        self._resolve_current(InterventionResolution.KEEP_CONTROL)

    def action_resolve_skip(self) -> None:
        self._resolve_current(InterventionResolution.SKIP_APPLICATION)

    def action_focus_answer(self) -> None:
        self.query_one("#answer-input", Input).focus()

    def action_open_browser(self) -> None:
        if (item := self._current) is None:
            self._set_status("Nothing to open: the inbox is empty.")
            return
        self._open_browser(item)

    def _resolve_current(
        self,
        resolution: InterventionResolution,
        *,
        payload: dict[str, str] | None = None,
    ) -> None:
        if (item := self._current) is None:
            self._set_status("Nothing to resolve: the inbox is empty.")
            return
        self._resolve(item, resolution, payload)

    @work(group="resolve", exclusive=True, exit_on_error=False)
    async def _resolve(
        self,
        item: InboxItem,
        resolution: InterventionResolution,
        payload: dict[str, str] | None,
    ) -> None:
        app = cast("TuiApp", self.app)
        async with app.container.repositories() as repos:
            attempt = await AttemptService(repos).resolve(
                item.attempt.id,
                item.intervention.id,
                resolution,
                payload=payload,
                manager=app.container.browser_hosts,
            )
        # A handoff whose host never came back stays open on purpose, so the
        # list has to be re-read rather than optimistically pruned.
        if attempt.pending_intervention is not None:
            self._set_status(
                f"Still waiting on {item.company or 'this application'}: "
                f"the Browser Host could not be reclaimed, so {resolution.value} "
                f"did not take effect."
            )
        else:
            self._set_status(f"Resolved {resolution.value}.")
        self._load()

    @work(group="handoff", exclusive=True, exit_on_error=False)
    async def _open_browser(self, item: InboxItem) -> None:
        """Ask the live host to surface the session, exactly as the API does."""
        app = cast("TuiApp", self.app)
        async with app.container.repositories() as repos:
            service = AttemptService(repos)
            attempt = await service.get(item.attempt.id)
            open_item = attempt.pending_intervention
            instruction = (
                open_item.instruction if open_item is not None else "Take over this application."
            )
            result = await service.activate_browser(
                attempt, manager=app.container.browser_hosts, instruction=instruction
            )
        self._set_status(str(result["detail"]))

    # -- events ------------------------------------------------------------

    @on(DataTable.RowHighlighted, "#inbox-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._current = self._entry_at(event.cursor_row)
        self._label_answer_box()

    def _label_answer_box(self) -> None:
        """Name the question in the answer box's placeholder.

        One input serves every row, so without this the user is asked to type
        into a box that does not say what it is answering.
        """
        item = self._current
        question = item.intervention.question_text if item is not None else None
        self.query_one("#answer-input", Input).placeholder = (
            question if question else "Answer the question, then press enter"
        )

    @on(DataTable.RowSelected, "#inbox-table")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter is the answer path when there is a question to answer.

        Answering is the only resolution that needs typed input, and asking
        someone to type into a box they were never sent to is the confusing
        case.
        """
        item = self._entry_at(event.cursor_row)
        if item is not None and item.intervention.question_text:
            self.action_focus_answer()

    @on(Input.Submitted, "#answer-input")
    def _answer_submitted(self, event: Input.Submitted) -> None:
        if (item := self._current) is None:
            self._set_status("Nothing to answer: the inbox is empty.")
            return
        answer = event.value.strip()
        self.query_one("#answer-input", Input).value = ""
        self._resolve(item, InterventionResolution.ANSWER, {"answer": answer})

    def _entry_at(self, index: int) -> InboxItem | None:
        """The inbox entry behind a table row index, or None if it is gone."""
        table = self.query_one("#inbox-table", DataTable)
        if not 0 <= index < table.row_count:
            return None
        key = table.ordered_rows[index].key.value
        return self._by_intervention_id.get(key) if key is not None else None

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)


def _opened_label(opened_at: datetime) -> str:
    """Local wall-clock time: a pause is read minutes after it opened."""
    return opened_at.astimezone().strftime(_OPENED_FORMAT)


__all__ = ["NeedsYouScreen"]
