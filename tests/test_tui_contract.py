"""Every TUI screen must mount, and the app must be able to reach all of them.

A screen that renders but whose worker crashes on mount is not covered. Each
test here asserts on content, not merely that a widget object exists, and the
app-level test drives real navigation rather than importing the screens
directly, so a screen that cannot be pushed is caught.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("textual")

from pathlib import Path

from textual.widgets import DataTable

import applyuminati.tui
from applyuminati.tui.app import TuiApp

#: Textual resolves a relative CSS_PATH against the module that defines the
#: class, so a test-local subclass would look for tests/styles.tcss.
_TCSS = str(Path(applyuminati.tui.__file__).parent / "styles.tcss")


class _Host(TuiApp):
    """A TuiApp that does not start the real attempt worker."""

    CSS_PATH = _TCSS

    async def on_mount(self) -> None:
        return None


#: The app-level key that must reach each screen. This is the integration
#: contract: a screen with no key is a screen a user cannot open.
SCREEN_KEYS = {
    "1": "dashboard",
    "2": "jobs",
    "3": "needs_you",
    "4": "sources",
    "5": "settings",
    "6": "profile",
}

#: module path, class name, and the kwargs its constructor needs. Region ids
#: are deliberately not listed: they would drift, and "mounted with content" is
#: the property that actually matters.
SCREENS = [
    ("applyuminati.tui.screens.dashboard", "DashboardScreen", {}),
    ("applyuminati.tui.screens.jobs", "JobsScreen", {}),
    ("applyuminati.tui.screens.job_detail", "JobDetailScreen", {"job_id": "missing-job"}),
    ("applyuminati.tui.screens.needs_you", "NeedsYouScreen", {}),
    ("applyuminati.tui.screens.sources", "SourcesScreen", {}),
    ("applyuminati.tui.screens.settings", "SettingsScreen", {}),
    ("applyuminati.tui.screens.profile", "ProfileScreen", {}),
]


async def _settle(pilot: Any, attempts: int = 60) -> None:
    for _ in range(attempts):
        await pilot.pause()
        await asyncio.sleep(0)


#: App short name -> the screen class it must land on.
_CLASS_FOR = {
    "dashboard": "DashboardScreen",
    "jobs": "JobsScreen",
    "needs_you": "NeedsYouScreen",
    "sources": "SourcesScreen",
    "settings": "SettingsScreen",
    "profile": "ProfileScreen",
}


@pytest.mark.parametrize(("module", "class_name", "kwargs"), SCREENS)
async def test_every_screen_module_exports_its_screen(module, class_name, kwargs) -> None:
    """A screen that cannot be imported is a screen a user cannot open."""
    import importlib

    mod = importlib.import_module(module)
    assert hasattr(getattr(mod, class_name), "compose"), f"{class_name} is not a screen"


async def _has_rendered_content(screen) -> bool:
    """True when the screen drew something a user could read."""
    from textual.widgets import Static

    for node in screen.query(Static):
        text = getattr(node, "content", None)
        if text and str(text).strip():
            return True
    return any(table.row_count or table.column_count for table in screen.query(DataTable))


@pytest.mark.parametrize(("module", "class_name", "kwargs"), SCREENS)
async def test_every_screen_mounts_and_renders_content(
    module, class_name, kwargs, container
) -> None:
    """Mount it for real. A crash in a worker leaves the screen empty."""
    import importlib

    mod = importlib.import_module(module)
    screen_class = getattr(mod, class_name)

    app = _Host(container)
    async with app.run_test(size=(120, 36)) as pilot:
        await app.push_screen(screen_class(**kwargs))
        await _settle(pilot)
        assert len(app.screen.children), f"{class_name} mounted no widgets"
        assert await _has_rendered_content(app.screen), (
            f"{class_name} rendered nothing readable; a worker probably crashed"
        )


async def test_the_app_does_not_autofocus_a_text_field() -> None:
    """A focused Input consumes printable keys and kills every nav binding."""
    assert TuiApp.AUTO_FOCUS is None


async def test_the_app_can_reach_every_screen(container) -> None:
    """Navigation is the integration point. Drive the real keys."""
    app = _Host(container)
    async with app.run_test(size=(120, 36)) as pilot:
        for key, name in SCREEN_KEYS.items():
            # A real keypress, not a direct call: a binding that exists but is
            # swallowed by a focused widget is still a broken binding.
            await pilot.press(key)
            await _settle(pilot)
            assert type(app.screen).__name__ == _CLASS_FOR[name], (
                f"key {key} did not reach {_CLASS_FOR[name]}, screen is {type(app.screen).__name__}"
            )


async def test_a_data_table_screen_declares_row_cursor(container) -> None:
    """RowSelected only fires with cursor_type='row'; the default is 'cell'."""
    from applyuminati.tui.screens.jobs import JobsScreen

    app = _Host(container)
    async with app.run_test(size=(120, 36)) as pilot:
        await app.push_screen(JobsScreen())
        await _settle(pilot)
        table = app.screen.query_one("#jobs-table", DataTable)
        assert table.cursor_type == "row"


async def test_no_screen_calls_asyncio_run() -> None:
    """Textual owns the event loop. asyncio.run inside it raises immediately."""
    from pathlib import Path

    import applyuminati.tui

    package = Path(applyuminati.tui.__file__).parent
    offenders = [
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if "asyncio.run(" in path.read_text()
    ]
    assert offenders == []


async def test_the_attempt_worker_does_not_leak_between_apps(container) -> None:
    """A leaked task makes the whole test session hang at teardown."""
    app = TuiApp(container)
    async with app.run_test():
        await app._flush_worker_start()
        task = app._worker_task
        assert task is not None
    assert task.cancelled() or task.done()
