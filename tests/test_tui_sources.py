"""The Sources and Settings screens.

Two things are asserted here that the CLI cannot do at all: configuring a
source's **options** (there is no ``--options`` flag on
``applyuminati sources enable``) and editing the search **strategy** without
hand-editing a profile. Both screens are also asserted on rendered content,
because a form that mounts with the right widgets but sends the wrong payload
still fails the user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("textual", reason="the tui extra is not installed")

from pydantic import BaseModel, Field
from textual.widgets import DataTable, Input, Static

from applyuminati.core.models.job import AtsVendor, SourceTier, VerificationState
from applyuminati.core.models.jsonresume import JsonResume, ResumeBasics
from applyuminati.core.models.profile import CareerProfile
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.sources.base import (
    SOURCE_REGISTRY,
    DiscoveryRequest,
    FreshnessResult,
    SourceMetadata,
    SourceResult,
    source_plugin,
)
from applyuminati.tui.app import TuiApp
from applyuminati.tui.screens.settings import SettingsScreen, parse_dial
from applyuminati.tui.screens.sources import SourcesScreen, parse_options

if TYPE_CHECKING:
    from textual.pilot import Pilot
    from textual.screen import Screen


#: A terminal tall enough that every button a test clicks is on screen. A
#: click below the fold raises OutOfBounds, which says nothing about the
#: behaviour under test.
_SIZE = (100, 40)


async def _click(pilot: Pilot, selector: str) -> None:
    """Click a button and let the worker it starts finish."""
    await pilot.click(selector)
    await _settle(pilot)


async def _settle(pilot: Pilot, *, rounds: int = 8) -> None:
    """Let the load worker run and the screen repaint."""
    for _ in range(rounds):
        await pilot.pause()


def _table(screen: SourcesScreen) -> DataTable:
    return screen.query_one("#sources-table", DataTable)


def _row_of(screen: SourcesScreen, slug: str) -> list[str]:
    table = _table(screen)
    for row in range(table.row_count):
        cells = [str(cell) for cell in table.get_row_at(row)]
        if cells[0] == slug:
            return cells
    raise AssertionError(f"no row for {slug!r}; table has {table.row_count} rows")


def _text(screen: Screen[Any], selector: str) -> str:
    return str(screen.query_one(selector, Static).content)


async def _show(app: TuiApp, pilot: Pilot, screen: Any) -> Any:
    await app.push_screen(screen)
    await _settle(pilot)
    assert app.screen is screen
    return screen


async def _select(pilot: Pilot, screen: SourcesScreen, slug: str) -> None:
    """Move the cursor onto a source and select it, as a user would."""
    table = _table(screen)
    row = next(
        (index for index in range(table.row_count) if str(table.get_row_at(index)[0]) == slug),
        None,
    )
    assert row is not None, f"no row for {slug!r}"
    # Clicking a button leaves focus on it, and "enter" would re-press it
    # rather than select the highlighted row.
    table.focus()
    table.move_cursor(row=row)
    await pilot.pause()
    await pilot.press("enter")
    await _settle(pilot)


def _profile() -> CareerProfile:
    return CareerProfile(id="p1", label="test", resume=JsonResume(basics=ResumeBasics(name="T")))


async def _seed_profile(container: Any) -> None:
    async with container.repositories() as repos:
        await repos.profiles.upsert(_profile())


def _stub_source(slug: str) -> Any:
    """A minimal ``JobSource`` for a test-registered plugin.

    Only ``metadata`` is read by ``SourceService``; ``health`` answers
    immediately and discovery returns nothing, so no test touches the network.
    """

    class _Stub:
        def __init__(self, settings: Any, options: dict[str, Any] | None = None) -> None:
            self._metadata = SourceMetadata(
                slug=slug,
                name=slug.title(),
                tier=SourceTier.DIRECT_ATS,
                ats=AtsVendor.GREENHOUSE,
            )

        @property
        def metadata(self) -> SourceMetadata:
            return self._metadata

        async def health(self) -> HealthReport:
            return HealthReport(plugin=slug, state=HealthState.HEALTHY)

        async def discover(self, request: DiscoveryRequest) -> SourceResult:
            return SourceResult(source=slug, jobs=[], failures=[])

        async def verify(self, job: Any) -> FreshnessResult:
            return FreshnessResult(job_id=job.source_job_id, state=VerificationState.UNVERIFIED)

    return _Stub


def _register(slug: str, *, options_schema: type[BaseModel] | None = None) -> Any:
    """Register a throwaway source plugin under ``slug``."""
    return source_plugin(
        slug=slug, name=slug.title(), factory=_stub_source(slug), options_schema=options_schema
    )


# ---------------------------------------------------------------------------
# The form is generated from the plugin's own schema
# ---------------------------------------------------------------------------


async def test_the_form_is_generated_from_the_plugins_schema(container: Any) -> None:
    """Greenhouse declares one array field; local feed declares two other shapes.

    A hardcoded Greenhouse field list would render one input here, not three
    across two different sources.
    """
    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SourcesScreen())
        await _select(pilot, screen, "greenhouse")
        assert screen.query_one("#opt-boards") is not None

        await _select(pilot, screen, "local_feed")
        assert screen.query_one("#opt-paths") is not None
        assert screen.query_one("#opt-default_company") is not None


async def test_a_source_without_a_schema_says_so_rather_than_showing_an_empty_form(
    container: Any,
) -> None:
    SOURCE_REGISTRY.register(_register("opaque"), replace=True)
    try:
        app = TuiApp(container)
        async with app.run_test(size=_SIZE) as pilot:
            screen = await _show(app, pilot, SourcesScreen())
            await _select(pilot, screen, "opaque")

            assert "no configurable options" in _text(screen, "#options-empty")
    finally:
        SOURCE_REGISTRY.unregister("opaque")


async def test_existing_options_are_prefilled_into_the_form(container: Any) -> None:
    async with container.repositories() as repos:
        await repos.sources.set_enabled("greenhouse", True, {"boards": ["acme", "globex"]})

    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SourcesScreen())
        await _select(pilot, screen, "greenhouse")

        assert screen.query_one("#opt-boards", Input).value == "acme, globex"


# ---------------------------------------------------------------------------
# Health, reported honestly
# ---------------------------------------------------------------------------


async def test_health_is_the_recorded_state_not_a_hardcoded_healthy(container: Any) -> None:
    """A source that has never been probed says so; it does not claim to be up."""
    async with container.repositories() as repos:
        await repos.sources.record_health(
            "greenhouse", HealthState.DEGRADED, "no boards configured"
        )

    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SourcesScreen())

        assert _row_of(screen, "greenhouse")[3] == "degraded"
        assert _row_of(screen, "lever")[3] == "unknown"


async def test_probing_reports_the_real_state_of_a_source_with_no_boards(container: Any) -> None:
    """Greenhouse with no boards is DEGRADED, and the screen says why.

    This is the check that would fail if the column were hardcoded to
    ``healthy``: nothing here touches the network, because the plugin can
    answer "no boards configured" from its own configuration.
    """
    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SourcesScreen())
        await _click(pilot, "#probe-health")

        assert _row_of(screen, "greenhouse")[3] == "degraded"
        await _select(pilot, screen, "greenhouse")
        assert "no boards configured" in _text(screen, "#source-detail")


async def test_enablement_is_read_from_persisted_state(container: Any) -> None:
    async with container.repositories() as repos:
        await repos.sources.set_enabled("lever", True)

    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SourcesScreen())

        assert _row_of(screen, "lever")[2] == "yes"
        assert _row_of(screen, "greenhouse")[2] == "no"


# ---------------------------------------------------------------------------
# Submitting options
# ---------------------------------------------------------------------------


async def test_enabling_a_source_through_the_form_persists_its_options(container: Any) -> None:
    """The whole point: options the CLI cannot set, set from the rendered form."""
    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SourcesScreen())
        await _select(pilot, screen, "greenhouse")
        screen.query_one("#opt-boards", Input).value = "example"
        await _click(pilot, "#save-source")

        async with container.repositories() as repos:
            state = await repos.sources.get("greenhouse")
        assert state is not None
        assert state.enabled is True
        assert state.options == {"boards": ["example"]}
        assert _row_of(screen, "greenhouse")[2] == "yes"


async def test_disabling_a_source_keeps_its_options_for_next_time(container: Any) -> None:
    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SourcesScreen())
        await _select(pilot, screen, "greenhouse")
        screen.query_one("#opt-boards", Input).value = "example"
        await _click(pilot, "#save-source")
        await _click(pilot, "#disable-source")

        async with container.repositories() as repos:
            state = await repos.sources.get("greenhouse")
        assert state is not None
        assert state.enabled is False
        assert state.options == {"boards": ["example"]}
        assert _row_of(screen, "greenhouse")[2] == "no"


def test_a_comma_separated_field_splits_strips_and_drops_blanks() -> None:
    schema = {"properties": {"boards": {"type": "array", "items": {"type": "string"}}}}
    assert parse_options(schema, {"boards": "a, b , c"}) == {"boards": ["a", "b", "c"]}


def test_an_empty_field_becomes_an_empty_list_not_a_list_of_one_blank() -> None:
    schema = {"properties": {"boards": {"type": "array", "items": {"type": "string"}}}}
    assert parse_options(schema, {"boards": ""}) == {"boards": []}
    assert parse_options(schema, {"boards": "   "}) == {"boards": []}
    assert parse_options(schema, {"boards": ","}) == {"boards": []}


def test_a_string_field_is_not_stripped_because_a_value_may_contain_spaces() -> None:
    schema = {"properties": {"name": {"type": "string"}}}
    assert parse_options(schema, {"name": " acme board "}) == {"name": " acme board "}


def test_a_nullable_string_field_becomes_none_when_left_blank() -> None:
    schema = {
        "properties": {
            "default_company": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
        }
    }
    assert parse_options(schema, {"default_company": ""}) == {"default_company": None}
    assert parse_options(schema, {"default_company": "Acme"}) == {"default_company": "Acme"}


def test_a_field_the_form_cannot_render_is_left_out_rather_than_guessed() -> None:
    schema = {"properties": {"retries": {"type": "integer"}, "boards": {"type": "string"}}}
    assert parse_options(schema, {"boards": "x"}) == {"boards": "x"}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


async def test_invalid_options_surface_the_configuration_error_not_a_generic_failure(
    container: Any,
) -> None:
    """A source that constrains a field, rejected through the rendered form."""

    class _StrictOptions(BaseModel):
        boards: list[str] = Field(default_factory=list, max_length=1)

    SOURCE_REGISTRY.register(_register("strict", options_schema=_StrictOptions), replace=True)
    try:
        app = TuiApp(container)
        async with app.run_test(size=_SIZE) as pilot:
            screen = await _show(app, pilot, SourcesScreen())
            await _select(pilot, screen, "strict")
            screen.query_one("#opt-boards", Input).value = "one, two"
            await _click(pilot, "#save-source")

            message = _text(screen, "#status")
            assert "invalid options for source plugin 'strict'" in message
            # The pydantic detail survives, so the user sees which field and
            # what it expected rather than a bare "invalid options".
            assert "boards" in message
            assert "at most 1" in message

            # A rejected payload must not be half-written.
            async with container.repositories() as repos:
                state = await repos.sources.get("strict")
            assert state is None or not state.enabled
    finally:
        SOURCE_REGISTRY.unregister("strict")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def test_the_settings_screen_shows_the_execution_mode_and_browser_read_only(
    container: Any,
) -> None:
    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SettingsScreen())

        assert "autonomous_submit" in _text(screen, "#execution-mode")
        assert "ego_lite" in _text(screen, "#browser-preference")


async def test_the_settings_screen_shows_the_persisted_strategy_after_a_change(
    container: Any,
) -> None:
    await _seed_profile(container)

    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SettingsScreen())
        assert screen.query_one("#dial-depth_bias", Input).value == "0.50"

        screen.query_one("#dial-depth_bias", Input).value = "0.90"
        await _click(pilot, "#save-strategy")

        assert screen.query_one("#dial-depth_bias", Input).value == "0.90"

        # And it really is in the profile, not just repainted in the widget.
        async with container.repositories() as repos:
            profile = await repos.profiles.get_active()
        assert profile is not None
        assert profile.strategy.depth_bias == 0.90
        assert profile.strategy.pages_per_source == 9


async def test_the_settings_screen_shows_a_strategy_that_was_never_edited(container: Any) -> None:
    await _seed_profile(container)

    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SettingsScreen())

        assert screen.query_one("#dial-application_volume_bias", Input).value == "0.35"
        assert screen.query_one("#dial-minimum_fit_score", Input).value == "0.55"


async def test_an_incoherent_strategy_is_refused_with_the_reason(container: Any) -> None:
    """skip_below_score above minimum_fit_score is a model invariant, not a UI one."""
    await _seed_profile(container)

    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SettingsScreen())
        screen.query_one("#dial-skip_below_score", Input).value = "0.99"
        screen.query_one("#dial-minimum_fit_score", Input).value = "0.20"
        await _click(pilot, "#save-strategy")

        assert "must not exceed" in _text(screen, "#status")

        async with container.repositories() as repos:
            profile = await repos.profiles.get_active()
        assert profile is not None
        assert profile.strategy.skip_below_score == 0.3


async def test_saving_a_strategy_without_a_profile_says_so(container: Any) -> None:
    app = TuiApp(container)
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _show(app, pilot, SettingsScreen())
        await _click(pilot, "#save-strategy")

        assert "import a profile" in _text(screen, "#status")


def test_a_dial_that_is_not_a_number_is_reported_rather_than_crashing() -> None:
    assert parse_dial("0.25") == 0.25
    with pytest.raises(ValueError, match="not a number"):
        parse_dial("nope")


def test_a_dial_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        parse_dial("1.5")
