"""The TUI must start with no server running.

Textual owns its own event loop, so nothing in ``applyuminati.tui`` may call
``asyncio.run``; the CLI helper ``_run_async`` is forbidden there. Long
operations are Textual workers.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="the tui extra is not installed")

from textual.widgets import Footer, Header


async def test_tui_app_starts_headless(container) -> None:
    # The container is injected rather than left to the app: a no-arg TuiApp
    # builds one on the real data directory, and that engine is never disposed,
    # so its aiosqlite worker thread throws at interpreter shutdown.
    from applyuminati.tui.app import TuiApp

    app = TuiApp(container)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#content") is not None
        assert app.query_one(Header) is not None
        assert app.query_one(Footer) is not None


async def test_the_tui_package_never_calls_asyncio_run() -> None:
    """Textual owns the loop; asyncio.run inside it raises immediately."""
    from pathlib import Path

    import applyuminati.tui

    package = Path(applyuminati.tui.__file__).parent
    offenders = [
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if "asyncio.run(" in path.read_text()
    ]
    assert offenders == []


async def test_the_tui_starts_and_stops_the_attempt_worker(container) -> None:
    """The API starts the worker in its lifespan. A TUI has no lifespan."""
    from applyuminati.tui.app import TuiApp

    app = TuiApp(container)
    async with app.run_test():
        await app._flush_worker_start()
        assert app._worker_task is not None
        assert not app._worker_task.done()
        # Hold the reference: on_unmount clears the attribute.
        task = app._worker_task
    assert task.cancelled() or task.done()


async def test_a_tui_command_is_registered() -> None:
    from applyuminati.cli.main import app as cli_app

    # Typer derives a command's name from the function; the decorator does not
    # always populate ``name``, so accept either.
    names = {
        command.name or command.callback.__name__.replace("_", "-")
        for command in cli_app.registered_commands
        if command.callback is not None
    }
    assert "tui" in names


async def test_the_app_owns_its_attempt_worker_task(container) -> None:
    from applyuminati.tui.app import TuiApp

    app = TuiApp(container)
    assert hasattr(app, "_worker_task")
    assert hasattr(app, "_worker_stop")
