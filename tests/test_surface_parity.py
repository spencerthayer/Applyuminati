"""The manifest is only worth something if it can fail.

Four surfaces, four tests, one per surface. Each one takes the non-``None``
addresses in :data:`applyuminati.surface_parity.CAPABILITIES` and checks them
against the live thing: the Typer command tree, the FastAPI route table, the
Textual bindings, and the React Router paths. Delete a command, change a verb,
rebind a digit, or delete a route, and the matching test goes red.

The manifest is read as a module attribute at call time rather than imported
by name, so a test run against a patched ``CAPABILITIES`` sees the patch.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest

from applyuminati import surface_parity

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_SRC = REPO_ROOT / "apps" / "web" / "src"
PARITY_DOC = REPO_ROOT / "docs" / "parity.md"

#: What a Typer command path may look like, so a manifest typo such as
#: "Jobs  list" fails as malformed rather than as merely absent.
_COMMAND_WORDS = re.compile(r"[a-z0-9][a-z0-9-]*( [a-z0-9][a-z0-9-]*)*")


def _claimed(field: str) -> list[tuple[str, Any]]:
    """Every ``(id, value)`` this manifest claims for one surface."""
    return [
        (capability.id, getattr(capability, field))
        for capability in surface_parity.CAPABILITIES
        if getattr(capability, field) is not None
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_command_tree() -> set[str]:
    """Every leaf command path, e.g. ``"jobs discover"``.

    Duck-typed on ``commands`` rather than ``isinstance(..., click.Group)``:
    Typer ships its own command classes that are not click's.
    """
    import typer

    from applyuminati.cli.main import app

    found: set[str] = set()
    stack: list[tuple[Any, str]] = [(typer.main.get_command(app), "")]
    while stack:
        group, prefix = stack.pop()
        for name, command in group.commands.items():
            path = f"{prefix} {name}".strip()
            if getattr(command, "commands", None) is not None:
                stack.append((command, path))
            else:
                found.add(path)
    return found


def test_cli_manifest_matches_command_tree() -> None:
    tree = _cli_command_tree()
    # If the walk silently stopped at the root, every claim below would fail
    # for the wrong reason and this message would never be read.
    assert len(tree) > 10, f"command tree looks truncated: {sorted(tree)}"
    assert "jobs discover" in tree

    missing = [f"{cid} -> {cmd}" for cid, cmd in _claimed("cli_command") if cmd not in tree]
    assert not missing, "manifest claims CLI commands that are not registered: " + ", ".join(
        missing
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _collect_routes(routes: Any, into: set[tuple[str, str]]) -> None:
    """Every ``(method, path)`` on the app, however the routes are nested.

    This FastAPI version parks an included router behind a wrapper object
    rather than splicing its routes into the app, so a flat loop over
    ``app.routes`` sees ten empty wrappers and claims the API has no routes at
    all. The wrapper still holds the real router; its routes carry the prefix
    already applied, so they are used verbatim.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        children = getattr(included, "routes", None)
        if children is None:
            children = getattr(route, "routes", None)
        if children:
            _collect_routes(children, into)
            continue
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        into.update((method, path) for method in methods)


def test_api_manifest_matches_routes(database) -> None:  # type: ignore[no-untyped-def]
    from applyuminati.api.app import create_app
    from applyuminati.core.settings import SecuritySettings
    from applyuminati.db.session import set_database
    from applyuminati.services.container import set_container

    # Same construction as tests/test_api.py: the container and the database are
    # process-wide singletons, so both have to be rebound or this test inherits
    # whichever install the previous test left behind. Auth stays off because
    # this test is about the route table, not about requests.
    set_container(None)
    set_database(database)
    app = create_app(
        database.settings.model_copy(update={"security": SecuritySettings(enabled=False)})
    )

    registered: set[tuple[str, str]] = set()
    _collect_routes(app.routes, registered)

    assert ("GET", "/api/v1/health") in registered
    assert ("POST", "/api/v1/jobs/{job_id}/apply") in registered

    missing = [
        f"{cid} -> {route}"
        for cid, route in _claimed("api_route")
        if (route.split(" ", 1)[0], route.split(" ", 1)[1]) not in registered
    ]
    assert not missing, "manifest claims API routes that are not registered: " + ", ".join(missing)


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------


def test_tui_manifest_matches_bindings() -> None:
    pytest.importorskip("textual")
    from textual.binding import Binding
    from textual.screen import Screen

    from applyuminati.tui.app import TuiApp

    actions: dict[str, str] = {
        b.key: str(b.action) for b in TuiApp.BINDINGS if isinstance(b, Binding)
    }

    assert "1" in actions

    problems: list[str] = []
    for cid, claimed in _claimed("tui_binding"):
        key, _, screen = str(claimed).partition(":")
        if key not in actions:
            problems.append(f"{cid} -> key {key!r} is not bound")
            continue
        if not screen:
            problems.append(f"{cid} -> {claimed!r} names no screen")
            continue
        if actions[key] != f"go('{screen}')":
            problems.append(f"{cid} -> key {key!r} runs {actions[key]!r}, not go({screen!r})")
        if screen not in TuiApp.SCREEN_PATHS:
            problems.append(f"{cid} -> screen {screen!r} is not in SCREEN_PATHS")
            continue
        module_name, _, class_name = TuiApp.SCREEN_PATHS[screen].partition(":")
        cls = getattr(importlib.import_module(module_name), class_name)
        if not (isinstance(cls, type) and issubclass(cls, Screen)):
            problems.append(f"{cid} -> {screen} is not a Screen")

    assert not problems, "manifest claims TUI bindings that do not hold: " + "; ".join(problems)


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------

# Route literals, not structure. App.tsx is a flat <Route path="..."/> list and
# Layout.tsx a flat <NavLink to="..."/> list, so a quoted-attribute scan reads
# exactly what React Router will match. If either file ever grows nested or
# computed routes this stops being true, and the failure will be loud: the
# assert below counts what it found.
_ROUTE_ATTRS = {
    WEB_SRC / "App.tsx": re.compile(r'\bpath="([^"]+)"'),
    WEB_SRC / "components" / "Layout.tsx": re.compile(r'\bto="([^"]+)"'),
}


def _web_paths() -> set[str]:
    paths: set[str] = set()
    for source, pattern in _ROUTE_ATTRS.items():
        assert source.is_file(), f"{source} is missing; the web surface moved"
        paths.update(pattern.findall(source.read_text(encoding="utf-8")))
    return paths


def test_web_manifest_matches_routes() -> None:
    declared = _web_paths()
    assert {"/", "/jobs", "/needs-you", "/profile", "/settings"} <= declared

    missing = [f"{cid} -> {route}" for cid, route in _claimed("web_route") if route not in declared]
    assert not missing, "manifest claims web routes that are not declared: " + ", ".join(missing)


# ---------------------------------------------------------------------------
# The generated document
# ---------------------------------------------------------------------------


def test_parity_doc_is_regenerated_from_the_manifest() -> None:
    assert PARITY_DOC.is_file(), "docs/parity.md is missing; run `make parity`"
    assert PARITY_DOC.read_text(encoding="utf-8") == surface_parity.render_markdown(), (
        "docs/parity.md has drifted from the manifest; run `make parity`"
    )


def test_manifest_ids_are_unique() -> None:
    ids = [c.id for c in surface_parity.CAPABILITIES]
    assert len(ids) == len(set(ids))
    assert all(c.summary for c in surface_parity.CAPABILITIES)


@pytest.mark.parametrize("field", ["cli_command", "api_route", "tui_binding", "web_route"])
def test_claimed_addresses_are_well_formed(field: str) -> None:
    """A malformed claim would otherwise pass by never matching anything real."""
    for cid, value in _claimed(field):
        if field == "api_route":
            method, _, path = str(value).partition(" ")
            assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"}, f"{cid}: {value}"
            assert path.startswith("/"), f"{cid}: {value}"
        elif field == "tui_binding":
            key, _, screen = str(value).partition(":")
            assert key, f"{cid}: {value}"
            assert screen, f"{cid}: {value}"
        elif field == "web_route":
            assert str(value).startswith("/"), f"{cid}: {value}"
        else:
            assert _COMMAND_WORDS.fullmatch(str(value)), f"{cid}: {value}"
