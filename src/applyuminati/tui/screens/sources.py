"""The Sources screen: enable a source and configure its options.

The CLI can toggle a source but cannot set its options — ``sources enable``
takes a slug and nothing else — and the WebUI renders no options form. This
screen closes that gap, and it does so **from the plugin's own JSON Schema**:
each plugin declares a Pydantic options model, and the form is built from
whatever that model turns into. A new source therefore needs no change here.

Parsing rules, which are the whole risk of a schema-driven form:

* ``array`` of ``string`` is one comma-separated field. An empty field is an
  empty *list*, never ``[""]`` — a list holding the empty string is a
  configured board token that matches nothing.
* ``string`` is passed through unstripped: a company name may legitimately
  contain leading or trailing spaces, and this screen is not entitled to
  rewrite what the user typed.
* A field whose type this form cannot render is omitted from the payload
  rather than guessed at, so the plugin's own default applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label, Static

from applyuminati.core.errors import ApplyuminatiError
from applyuminati.db.repositories.sources import SourceState
from applyuminati.services.source_service import SourceService
from applyuminati.services.views import SourceView

if TYPE_CHECKING:
    from applyuminati.services.container import ServiceContainer
    from applyuminati.tui.app import TuiApp

#: Column order of the sources table. Tests and the status line both index it.
_COLUMNS = ("Source", "Tier", "Enabled", "Health", "Last run")


def _container(screen: Screen[Any]) -> ServiceContainer:
    """The process container, narrowed to the concrete app."""
    return cast("TuiApp", screen.app).container


def _json_type(spec: dict[str, Any]) -> str | None:
    """The declared type of a JSON Schema property, or ``None`` if there is none.

    ``anyOf`` unions are resolved to their first non-null member, because a
    nullable string is still editable as a text field; the nullability is kept
    separately by :func:`_is_nullable`.
    """
    declared = spec.get("type")
    if isinstance(declared, str):
        return declared
    for member in spec.get("anyOf") or []:
        if isinstance(member, dict) and member.get("type") != "null":
            resolved = _json_type(member)
            if resolved is not None:
                return resolved
    return None


def _is_nullable(spec: dict[str, Any]) -> bool:
    return any(
        isinstance(member, dict) and member.get("type") == "null"
        for member in spec.get("anyOf") or []
    )


def _is_string_list(spec: dict[str, Any]) -> bool:
    return _json_type(spec) == "array" and _json_type(spec.get("items") or {}) == "string"


def _properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The schema's properties, each coerced to a dict so the form can read them."""
    raw = schema.get("properties")
    if not isinstance(raw, dict):
        return {}
    return {str(name): spec if isinstance(spec, dict) else {} for name, spec in raw.items()}


def parse_options(schema: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Turn the rendered form's strings into the payload the plugin validates.

    Only fields the form can honestly represent appear in the result.
    """
    payload: dict[str, Any] = {}
    for name, spec in _properties(schema).items():
        value = values.get(name, "")
        if _is_string_list(spec):
            payload[name] = [part.strip() for part in value.split(",") if part.strip()]
        elif _json_type(spec) == "string":
            payload[name] = None if (value == "" and _is_nullable(spec)) else value
    return payload


def _format(value: Any) -> str:
    """Render a stored option value back into its one-line form-field text."""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return "" if value is None else str(value)


class SourcesScreen(Screen[None]):
    """List every registered source, and edit the selected one's options."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+p", "probe_health", "Probe health"),
    ]

    #: The table must not eat the whole screen: the options form and its save
    #: button live below it, and a button below the fold is a dead button.
    DEFAULT_CSS = """
    SourcesScreen {
        layout: vertical;
    }
    #sources-table {
        height: 1fr;
        min-height: 5;
    }
    #source-pane {
        height: 2fr;
        border: round $accent;
        padding: 0 1;
    }
    #source-detail, #options-empty {
        height: auto;
    }
    #options-form Input {
        width: 100%;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._views: list[SourceView] = []
        self._recorded: dict[str, SourceState] = {}
        self._selected: str | None = None

    def compose(self) -> ComposeResult:
        yield DataTable(id="sources-table", cursor_type="row")
        with VerticalScroll(id="source-pane"):
            yield Static("", id="source-detail")
            yield Static("", id="options-empty")
            # The form is rebuilt on every selection, so the actions live
            # outside it: a rebuild must not delete the save button.
            with Vertical(id="options-form"):
                pass
            with Horizontal(id="source-actions"):
                yield Button("Save and enable", id="save-source", variant="primary")
                yield Button("Disable", id="disable-source")
                yield Button("Probe health", id="probe-health")
        yield Static("", id="status")

    async def on_mount(self) -> None:
        table = self.query_one("#sources-table", DataTable)
        table.add_columns(*_COLUMNS)
        self.refresh_sources()

    @on(Button.Pressed, "#probe-health")
    def _probe_pressed(self) -> None:
        self.query_one("#status", Static).update("Probing source health...")
        self.refresh_sources(probe=True)

    # -- loading -----------------------------------------------------------

    @work(exclusive=True, exit_on_error=False)
    async def refresh_sources(self, *, probe: bool = False) -> None:
        """Reload the list and repaint.

        Health comes from what was last *recorded*, not from a live probe on
        every keystroke: probing reaches the network, and a screen that hangs
        while three boards time out is worse than one that shows the last
        known answer and offers an explicit re-check. ``exit_on_error=False``
        because a probe that blows up must not take the application with it.
        """
        container = _container(self)
        try:
            async with container.read_repositories() as repos:
                views = await SourceService(repos, container.settings).list(probe_health=probe)
                recorded = await repos.sources.all()
        except ApplyuminatiError as exc:
            self.query_one("#status", Static).update(exc.message)
            return
        self._views = views
        self._recorded = recorded
        self._render_table()
        if self._selected is not None:
            await self.render_form(self._selected)

    def _health_of(self, view: SourceView) -> tuple[str, str]:
        """The state and detail to show: a live probe wins over what is recorded."""
        if view.health is not None:
            return view.health.state.value, view.health.detail
        state = self._recorded.get(view.slug)
        if state is None:
            return "unknown", ""
        return state.health_state.value, state.health_detail

    def _render_table(self) -> None:
        table = self.query_one("#sources-table", DataTable)
        table.clear()
        for view in self._views:
            health, _ = self._health_of(view)
            table.add_row(
                view.slug,
                view.tier,
                "yes" if view.enabled else "no",
                health,
                str(view.last_run_at) if view.last_run_at else "—",
                key=view.slug,
            )

    # -- selection ---------------------------------------------------------

    @on(DataTable.RowSelected)
    async def _row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if isinstance(key, str):
            await self.select_source(key)

    async def select_source(self, slug: str) -> None:
        view = next((v for v in self._views if v.slug == slug), None)
        if view is None:
            return
        self._selected = slug
        state, detail_text = self._health_of(view)
        lines = [view.description or "No description.", f"Health: {state}"]
        if detail_text:
            lines.append(detail_text)
        self.query_one("#source-detail", Static).update("\n".join(lines))
        await self.render_form(slug)

    async def render_form(self, slug: str) -> None:
        """Rebuild the option inputs for one source, from its own schema."""
        view = next((v for v in self._views if v.slug == slug), None)
        form = self.query_one("#options-form", Vertical)
        empty = self.query_one("#options-empty", Static)
        # Removal must complete before the rebuild: a widget id is unique
        # within the screen, so mounting a replacement while the old input is
        # still registered raises DuplicateIds.
        await form.remove_children()

        schema = view.options_schema if view is not None else None
        if not schema or not schema.get("properties"):
            empty.update("This source has no configurable options.")
            empty.display = True
            return
        empty.display = False

        current = view.options if view is not None else {}
        fields: list[Widget] = []
        for name, spec in _properties(schema).items():
            if not (_is_string_list(spec) or _json_type(spec) == "string"):
                continue
            raw_title = spec.get("title")
            title = raw_title if isinstance(raw_title, str) else name
            hint = "comma-separated" if _is_string_list(spec) else "text"
            fields.append(Label(title))
            fields.append(
                Input(value=_format(current.get(name)), placeholder=hint, id=f"opt-{name}")
            )
        if fields:
            await form.mount(*fields)

    # -- saving ------------------------------------------------------------

    def _form_values(self) -> dict[str, str]:
        return {
            widget.id.removeprefix("opt-"): widget.value  # type: ignore[union-attr]
            for widget in self.query("#options-form Input")
            if isinstance(widget, Input) and widget.id is not None
        }

    @on(Button.Pressed, "#save-source")
    def _save_pressed(self) -> None:
        slug = self._selected
        if slug is None:
            return
        view = next((v for v in self._views if v.slug == slug), None)
        schema = view.options_schema if view is not None else None
        options = parse_options(schema or {}, self._form_values())
        self.save_source(slug, True, options=options)

    @on(Button.Pressed, "#disable-source")
    def _disable_pressed(self) -> None:
        if self._selected is not None:
            self.save_source(self._selected, False)

    @work(exclusive=True, exit_on_error=False)
    async def save_source(
        self, slug: str, enabled: bool, *, options: dict[str, Any] | None = None
    ) -> None:
        """Persist enablement and options, reporting the service's own message.

        The ``ConfigurationError`` text is what tells a user *which* field their
        board token failed validation on, so it is shown verbatim rather than
        replaced with "save failed".
        """
        container = _container(self)
        try:
            async with container.repositories() as repos:
                view = await SourceService(repos, container.settings).set_enabled(
                    slug, enabled, options=options
                )
        except ApplyuminatiError as exc:
            self.query_one("#status", Static).update(exc.message)
            return
        verb = "Enabled" if enabled else "Disabled"
        self.query_one("#status", Static).update(f"{verb} {view.name}.")
        self._views = [v for v in self._views if v.slug != slug] + [view]
        self._views.sort(key=lambda v: v.slug)
        self._render_table()
        await self.render_form(slug)


__all__ = ["SourcesScreen", "parse_options"]
