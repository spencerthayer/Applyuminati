"""The dashboard: ``applyuminati status`` as a screen.

Same numbers, same labels and same wording as the CLI's ``status`` command,
because a dashboard that disagrees with the terminal it was launched from is
worse than no dashboard. Everything is a read through
:class:`~applyuminati.services.dashboard_service.DashboardService` in a Textual
worker, since Textual owns the event loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, Vertical
from textual.screen import Screen
from textual.widgets import Label, Static

from applyuminati.services.dashboard_service import DashboardService
from applyuminati.services.views import DashboardView

if TYPE_CHECKING:
    from applyuminati.services.container import ServiceContainer
    from applyuminati.tui.app import TuiApp

#: What a breakdown with nothing in it says. Blank space would read as a bug.
NO_RECOMMENDATIONS = "By recommendation: none yet"
NO_RUN = "Latest run: none yet"


class StatTile(Vertical):
    """One labelled number. The grid does the layout; a tile is just two lines."""

    DEFAULT_CSS = """\
    StatTile {
        height: auto;
        padding: 0 1;
        border: round $panel;
    }
    StatTile .stat-value {
        text-style: bold;
    }
    StatTile .stat-label {
        color: $text-muted;
    }
    """

    def __init__(self, tile_id: str, label: str) -> None:
        super().__init__(id=tile_id)
        self._label = label
        #: Zero, not "": a tile must never render as a blank box.
        self._value = "0"

    def compose(self) -> ComposeResult:
        yield Label(self._value, classes="stat-value")
        yield Label(self._label, classes="stat-label")

    def set_value(self, value: str) -> None:
        self._value = value
        self.query_one(".stat-value", Label).update(value)


class DashboardScreen(Screen[None]):
    """Pipeline counts, the recommendation breakdown, and the latest run."""

    DEFAULT_CSS = """\
    DashboardScreen {
        align: left top;
    }
    DashboardScreen #dashboard-loading {
        color: $text-muted;
        height: 1;
    }
    DashboardScreen #dashboard-error {
        color: $error;
        height: auto;
    }
    /* A grid, not nesting: six numbers are a 2D shape, and Textual's layout
       guide reaches for a grid before anything else. `height: auto` lets the
       rows size themselves; an explicit `grid-rows` would collapse them. */
    DashboardScreen #stat-grid {
        layout: grid;
        grid-size: 3;
        grid-gutter: 1 2;
        height: auto;
    }
    DashboardScreen #detail-grid {
        layout: grid;
        grid-size: 2;
        grid-gutter: 0 2;
        height: auto;
        margin-top: 1;
    }
    DashboardScreen #detail-grid Static {
        height: auto;
    }
    """

    #: ``(widget id, label)`` in reading order. The CLI prints these six in
    #: this order, so the screen does too.
    TILES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("stat-total-jobs", "Total jobs"),
        ("stat-shortlisted", "Shortlisted"),
        ("stat-ready", "Ready"),
        ("stat-submitted", "Submitted"),
        ("stat-needs-attention", "Needs attention"),
        ("stat-scored", "Scored"),
    )

    BINDINGS: ClassVar[list[BindingType]] = [Binding("r", "load_dashboard", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Static("Loading status...", id="dashboard-loading")
        yield Static("", id="dashboard-error")
        with Grid(id="stat-grid"):
            for tile_id, label in self.TILES:
                yield StatTile(tile_id, label)
        with Grid(id="detail-grid"):
            yield Static(NO_RECOMMENDATIONS, id="recommendation-breakdown")
            yield Static(NO_RUN, id="latest-run")

    def on_mount(self) -> None:
        self.query_one("#dashboard-error", Static).display = False
        self.load_dashboard()

    def action_load_dashboard(self) -> None:
        self.load_dashboard()

    def _container(self) -> ServiceContainer:
        """The process container, narrowed to the concrete app."""
        return cast("TuiApp", self.app).container

    @work(exclusive=True, exit_on_error=False)
    async def load_dashboard(self) -> None:
        """Read the dashboard and render it. Never takes the app down."""
        error = self.query_one("#dashboard-error", Static)
        error.display = False
        self.query_one("#dashboard-loading", Static).display = True
        try:
            async with self._container().read_repositories() as repos:
                view = await DashboardService(repos).build()
        except Exception as exc:
            error.update(f"Could not load status: {exc}")
            error.display = True
            self.query_one("#dashboard-loading", Static).display = False
            return
        self._apply(view)
        self.query_one("#dashboard-loading", Static).display = False

    def _apply(self, view: DashboardView) -> None:
        counts = {
            "stat-total-jobs": str(view.total_jobs),
            "stat-shortlisted": str(view.shortlisted),
            "stat-ready": str(view.ready),
            "stat-submitted": str(view.submitted),
            "stat-needs-attention": str(view.needs_attention),
            # The CLI prints "Scored: {scored} / {total_jobs}".
            "stat-scored": f"{view.scored} / {view.total_jobs}",
        }
        for tile_id, _label in self.TILES:
            self.query_one(f"#{tile_id}", StatTile).set_value(counts[tile_id])
        self.query_one("#recommendation-breakdown", Static).update(self._recommendations(view))
        self.query_one("#latest-run", Static).update(self._run(view))

    @staticmethod
    def _recommendations(view: DashboardView) -> str:
        if not view.by_recommendation:
            return NO_RECOMMENDATIONS
        lines = ["By recommendation:"]
        lines += [f"  {rec}: {count}" for rec, count in sorted(view.by_recommendation.items())]
        return "\n".join(lines)

    @staticmethod
    def _run(view: DashboardView) -> str:
        run = view.latest_run
        if run is None:
            return NO_RUN
        return f"Latest run: {run.kind} ({run.state.value})\nStats: {run.stats}"


__all__ = ["NO_RECOMMENDATIONS", "NO_RUN", "DashboardScreen", "StatTile"]
