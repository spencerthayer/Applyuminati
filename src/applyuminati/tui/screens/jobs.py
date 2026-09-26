"""The Jobs screen: a browsable, filterable list of everything discovered.

Two things here are load-bearing. Loading runs in a Textual worker, because
``App.run`` owns the event loop, so no helper may start its own loop; and
each row's DataTable key *is* the job id, so selecting a row costs no second
query — :meth:`JobsScreen.select_row` hands the id straight
to whoever pushed the detail screen.

The screen deliberately does not know what the detail screen is. It emits
:class:`JobSelected` and lets the app decide where that goes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from applyuminati.services.job_service import JobService
from applyuminati.services.views import JobView

if TYPE_CHECKING:
    from applyuminati.tui.app import TuiApp

#: Ceiling on a single page. A search is a filter, not a paginator, so this is
#: deliberately large enough that a human never notices hitting it.
_PAGE_LIMIT = 200

#: Shown wherever the data is genuinely absent, never as a stand-in for a
#: score that was not computed.
_ABSENT = "—"

_COLUMNS = ("Company", "Title", "Location", "Score", "Recommendation", "State")


class JobSelected(Message):
    """A row was activated. The app decides what to do with the id."""

    def __init__(self, job_id: str) -> None:
        super().__init__()
        self.job_id = job_id


class JobsScreen(Screen[None]):
    """Search, browse, and pick a job."""

    BINDINGS: ClassVar[list[BindingType]] = [
        # ``escape`` is deliberately left free: the app owns "go back".
        Binding("slash", "focus_search", "Search", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._query: str = ""

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="content"):
            yield Input(placeholder="Search company or title…", id="search")
            yield DataTable(id="jobs-table", cursor_type="row")
            yield Static("", id="empty-state")
        yield Static("", id="status")

    async def on_mount(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        table.add_columns(*_COLUMNS)
        self.query_one("#empty-state", Static).display = False
        self._load()

    # -- loading ----------------------------------------------------------

    @work(exclusive=True, exit_on_error=False)
    async def _load(self) -> None:
        """Rebuild the table from the service.

        ``exclusive`` is what debounces the search box: each keystroke cancels
        the query still in flight rather than racing it to the screen.
        """
        app = cast("TuiApp", self.app)
        query = self._query
        async with app.container.read_repositories() as repos:
            page = await JobService(repos).list(
                query=query or None, limit=_PAGE_LIMIT, sort="discovered_at"
            )
        self._populate(page.items, total=page.total, query=query)

    def _populate(self, views: list[JobView], *, total: int, query: str) -> None:
        table = self.query_one("#jobs-table", DataTable)
        table.clear()
        for view in views:
            table.add_row(*_cells(view), key=view.job.id)

        empty = self.query_one("#empty-state", Static)
        empty.display = not views
        if not views:
            empty.update(f'No jobs match "{query}".' if query else "No jobs discovered yet.")

        shown = len(views)
        noun = "job" if total == 1 else "jobs"
        summary = f"{shown} of {total} {noun}" if shown != total else f"{total} {noun}"
        self.query_one("#status", Static).update(summary)

    # -- interaction ------------------------------------------------------

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self._query = event.value.strip()
        self._load()

    @on(DataTable.RowSelected, "#jobs-table")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        job_id = event.row_key.value
        if job_id is not None:
            self.post_message(JobSelected(job_id))

    def select_row(self) -> str | None:
        """Report the row under the cursor and return its job id.

        The id is the row's DataTable key, so this is a dict lookup, not a
        query. The app calls this to open the detail screen; row activation
        goes through :class:`JobSelected` instead.
        """
        table = self.query_one("#jobs-table", DataTable)
        row = table.cursor_row
        if not 0 <= row < table.row_count:
            return None
        job_id = table.ordered_rows[row].key.value
        if job_id is None:
            return None
        self.post_message(JobSelected(job_id))
        return job_id

    # -- actions ----------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()


def _cells(view: JobView) -> tuple[str, ...]:
    """Flatten a :class:`JobView` into the table's columns."""
    job = view.job
    location = job.locations[0].display() if job.locations else _ABSENT
    score = view.score
    return (
        job.company,
        job.title,
        location,
        f"{score.overall:.2f}" if score else _ABSENT,
        score.recommendation.value if score else _ABSENT,
        view.application_state.value if view.application_state else _ABSENT,
    )


__all__ = ["JobSelected", "JobsScreen"]
