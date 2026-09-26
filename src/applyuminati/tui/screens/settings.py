"""The Settings screen: the search strategy dials, and the read-only runtime.

Strategy is persisted through :class:`SettingsService`, the same service
``PUT /api/v1/settings/strategy`` uses, so the TUI and the API cannot drift
into two different notions of what "balanced" means. Values are stored as
exact numbers; this screen shows and edits the numbers themselves.

Execution mode and browser preference come from ``Settings`` and are shown
read-only on purpose: they are process configuration, decided at install time,
and pretending to edit them here would be a lie about where the change lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Static

from applyuminati.core.errors import ApplyuminatiError
from applyuminati.core.strategy import SearchStrategy
from applyuminati.services.settings_service import SettingsService

if TYPE_CHECKING:
    from applyuminati.services.container import ServiceContainer
    from applyuminati.tui.app import TuiApp

#: The dials a user actually turns, with the sentence that explains each one.
#: Order is the order they are rendered in.
_DIALS: tuple[tuple[str, str], ...] = (
    ("depth_bias", "Search depth (0 = fast and shallow, 1 = slow and thorough)"),
    ("application_volume_bias", "Application volume (0 = few and precise, 1 = high volume)"),
    ("title_exploration", "Title exploration (0 = exact titles, 1 = adjacent titles)"),
    ("minimum_fit_score", "Minimum fit score to shortlist"),
    ("minimum_evidence_confidence", "Minimum evidence confidence"),
    ("skip_below_score", "Skip below this score outright"),
)


def _container(screen: Screen[Any]) -> ServiceContainer:
    """The process container, narrowed to the concrete app."""
    return cast("TuiApp", screen.app).container


def parse_dial(raw: str) -> float:
    """One dial, validated before it can reach the profile.

    The model validates ranges too, but a message naming the offending dial and
    saying what was expected beats a Pydantic traceback.
    """
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{raw!r} is not a number"
        raise ValueError(msg) from exc
    if not 0.0 <= value <= 1.0:
        msg = f"{value} is outside the range: a dial runs between 0 and 1"
        raise ValueError(msg)
    return value


class SettingsScreen(Screen[None]):
    """Edit the search strategy; show the execution mode and browser choice."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    DEFAULT_CSS = """
    SettingsScreen {
        layout: vertical;
    }
    #settings-body {
        height: 1fr;
        padding: 0 1;
    }
    #execution-mode, #browser-preference, #status {
        height: auto;
    }
    #strategy-form Input {
        width: 40;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="settings-body"):
            yield Static("", id="execution-mode")
            yield Static("", id="browser-preference")
            with Vertical(id="strategy-form"):
                yield Label("Search strategy")
                for name, description in _DIALS:
                    yield Label(description)
                    yield Input(id=f"dial-{name}")
                yield Button("Save strategy", id="save-strategy", variant="primary")
        yield Static("", id="status")

    async def on_mount(self) -> None:
        self.reload_settings()

    @work(exclusive=True, exit_on_error=False)
    async def reload_settings(self) -> None:
        container = _container(self)
        async with container.read_repositories() as repos:
            svc = SettingsService(repos, container.settings)
            snapshot = await svc.snapshot()
        strategy: SearchStrategy = snapshot["strategy"]
        self.query_one("#execution-mode", Static).update(
            f"Execution mode: {snapshot['execution_mode']} (read-only; set in config)"
        )
        preferred = ", ".join(snapshot["browser_preferred"]) or "none"
        self.query_one("#browser-preference", Static).update(
            f"Browser preference: {preferred} (read-only; set in config)"
        )
        for name, _ in _DIALS:
            self.query_one(f"#dial-{name}", Input).value = f"{getattr(strategy, name):.2f}"
        self.query_one("#status", Static).update(
            f"Strategy: {strategy.pages_per_source} page(s) per source per run."
        )

    def _dial_values(self) -> dict[str, float]:
        return {
            name: parse_dial(self.query_one(f"#dial-{name}", Input).value) for name, _ in _DIALS
        }

    @on(Button.Pressed, "#save-strategy")
    def _save_pressed(self) -> None:
        try:
            updates = self._dial_values()
        except ValueError as exc:
            self.query_one("#status", Static).update(str(exc))
            return
        self.save_strategy(updates)

    @work(exclusive=True, exit_on_error=False)
    async def save_strategy(self, updates: dict[str, float]) -> None:
        container = _container(self)
        try:
            async with container.repositories() as repos:
                svc = SettingsService(repos, container.settings)
                current = await svc.strategy()
                # model_validate, not model_copy: the threshold-ordering
                # invariant is a model check and must run before the write.
                strategy = await svc.update_strategy(
                    strategy=SearchStrategy.model_validate({**current.model_dump(), **updates})
                )
        except ApplyuminatiError as exc:
            self.query_one("#status", Static).update(exc.message)
            return
        except ValueError as exc:
            self.query_one("#status", Static).update(f"Invalid strategy: {exc}")
            return
        for name in updates:
            self.query_one(f"#dial-{name}", Input).value = f"{getattr(strategy, name):.2f}"
        self.query_one("#status", Static).update(
            f"Saved. Strategy searches {strategy.pages_per_source} page(s) per source per run."
        )


__all__ = ["SettingsScreen", "parse_dial"]
