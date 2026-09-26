"""One manifest of what each user-facing surface can do.

Four surfaces ship this product: the Typer CLI (``cli/main.py``), the FastAPI
service (``api/``), the Textual TUI (``tui/``) and the React web app
(``apps/web``). They were each built against whatever their own layer happened
to expose, so they drifted: the CLI has no inbox, the API has no capability
matrix, the TUI cannot discover or score, the web app cannot export a profile.
This module is the single statement of that, in plain data, with no imports
beyond the standard library — Textual in particular is an optional extra, and a
contract that cannot be imported without it cannot be the contract for it.

The fields are *addresses*, not feature flags, which is what makes them
checkable:

``cli_command``
    The Typer command path that does the thing, e.g. ``"jobs discover"``.
    ``None`` when the CLI cannot do it at all.
``api_route``
    ``"METHOD /path"`` as registered on the app, e.g.
    ``"GET /api/v1/jobs"``. Path parameters stay in FastAPI's ``{}`` form so
    the string is comparable with ``route.path``.
``tui_binding``
    ``"<key>:<screen>"`` — the app-level key that takes you to the screen that
    does the thing, e.g. ``"2:jobs"``. The screen half is checked against
    ``TuiApp.SCREEN_PATHS`` and the screen class is imported, so a manifest
    row cannot name a screen that does not exist. ``None`` when no key gets
    you there. (A few capabilities then take one further in-screen key — the
    Job Detail screen binds ``a`` to apply — and the summary says so.)
``web_route``
    The React Router path the user navigates to, e.g. ``"/jobs/:id"``. A
    capability that is a control inside an existing page takes that page's
    path; ``None`` when the web app cannot do it at all.

``None`` is a finding, not a gap in the file. tests/test_surface_parity.py
asserts the non-``None`` values against the live surfaces, so a row cannot
claim a command, route, key, or path that has gone away. ``make parity``
renders this module to docs/parity.md, and a test asserts the checked-in
document is byte-identical to the render, so the human-readable view cannot
drift from this one either.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CAPABILITIES", "Capability", "render_markdown"]


@dataclass(frozen=True, slots=True)
class Capability:
    """One thing a user can do, and where they can do it."""

    id: str
    summary: str
    cli_command: str | None = None
    api_route: str | None = None
    tui_binding: str | None = None
    web_route: str | None = None

    @property
    def everywhere(self) -> bool:
        """True when every surface can do this."""
        return None not in (
            self.cli_command,
            self.api_route,
            self.tui_binding,
            self.web_route,
        )


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="initialise",
        summary="Create a new install: data directories, schema, config file, password.",
        cli_command="init",
    ),
    Capability(
        id="run-doctor",
        summary=(
            "Check that the install works: database, source backends, LLM, config. "
            "The API route is the backend half only, and there is no whole-install "
            "check on the TUI or the web app; both show per-backend health instead."
        ),
        cli_command="doctor",
        api_route="GET /api/v1/health/backends",
    ),
    Capability(
        id="capabilities-matrix",
        summary="Print the plugin capability and maturity matrix.",
        cli_command="capabilities",
    ),
    Capability(
        id="dashboard",
        summary="Pipeline totals, recommendation split, and the latest run.",
        cli_command="status",
        api_route="GET /api/v1/dashboard",
        tui_binding="1:dashboard",
        web_route="/",
    ),
    Capability(
        id="list-sources",
        summary="List the registered job sources and their health.",
        cli_command="sources list",
        api_route="GET /api/v1/sources",
        tui_binding="4:sources",
        web_route="/settings",
    ),
    Capability(
        id="enable-source",
        summary="Turn a job source on.",
        cli_command="sources enable",
        api_route="POST /api/v1/sources/{slug}/enable",
        tui_binding="4:sources",
        web_route="/settings",
    ),
    Capability(
        id="disable-source",
        summary="Turn a job source off.",
        cli_command="sources disable",
        api_route="POST /api/v1/sources/{slug}/disable",
        tui_binding="4:sources",
        web_route="/settings",
    ),
    Capability(
        id="source-options",
        summary=(
            "Per-source query, location, and company filters. The API carries them "
            "in the enable body; the CLI can only set them in config.toml."
        ),
        api_route="POST /api/v1/sources/{slug}/enable",
        tui_binding="4:sources",
        web_route="/settings",
    ),
    Capability(
        id="discover",
        summary="Fetch new jobs from the enabled sources.",
        cli_command="jobs discover",
        api_route="POST /api/v1/jobs/discover",
        web_route="/",
    ),
    Capability(
        id="list-jobs",
        summary="Browse and filter the jobs that have been discovered.",
        cli_command="jobs list",
        api_route="GET /api/v1/jobs",
        tui_binding="2:jobs",
        web_route="/jobs",
    ),
    Capability(
        id="job-detail",
        summary="Everything about one job, with its fit score.",
        api_route="GET /api/v1/jobs/{job_id}",
        tui_binding="2:jobs",
        web_route="/jobs/:id",
    ),
    Capability(
        id="score",
        summary="Score jobs against the active profile.",
        cli_command="jobs score",
        api_route="POST /api/v1/jobs/score",
        web_route="/jobs",
    ),
    Capability(
        id="apply",
        summary="Start an application attempt for a job. In the TUI: key 2, then a.",
        cli_command="applications apply",
        api_route="POST /api/v1/jobs/{job_id}/apply",
        tui_binding="2:jobs",
        web_route="/jobs/:id",
    ),
    Capability(
        id="execution-mode",
        summary=(
            "Choose research_only versus submit. The CLI and web pick it per "
            "application; the TUI applies with the configured mode and offers no "
            "choice."
        ),
        cli_command="applications apply",
        api_route="POST /api/v1/jobs/{job_id}/apply",
        web_route="/jobs/:id",
    ),
    Capability(
        id="list-applications",
        summary="Browse applications and where each one has got to.",
        cli_command="applications list",
        api_route="GET /api/v1/applications",
        web_route="/applications",
    ),
    Capability(
        id="needs-you",
        summary="The handoff queue: attempts stopped, waiting on a human.",
        api_route="GET /api/v1/needs-you",
        tui_binding="3:needs_you",
        web_route="/needs-you",
    ),
    Capability(
        id="resolve-handoff",
        summary="Answer, skip, or keep control of a waiting attempt.",
        api_route="POST /api/v1/needs-you/{attempt_id}/{intervention_id}",
        tui_binding="3:needs_you",
        web_route="/needs-you",
    ),
    Capability(
        id="open-browser-handoff",
        summary="Hand the attempt to a paired browser host.",
        api_route="POST /api/v1/needs-you/{attempt_id}/open-browser",
        tui_binding="3:needs_you",
        web_route="/needs-you",
    ),
    Capability(
        id="import-profile",
        summary="Import a JSON Resume as the active profile.",
        cli_command="profile import",
        api_route="POST /api/v1/profile/import",
        tui_binding="6:profile",
        web_route="/profile",
    ),
    Capability(
        id="export-profile",
        summary="Write the active profile back out as a JSON Resume.",
        cli_command="profile export",
    ),
    Capability(
        id="view-profile",
        summary="Read the active profile and its claim levels.",
        api_route="GET /api/v1/profile",
        tui_binding="6:profile",
        web_route="/profile",
    ),
    Capability(
        id="edit-preferences",
        summary="Change target titles, locations, seniority, and compensation.",
        api_route="PUT /api/v1/profile/preferences",
        web_route="/profile",
    ),
    Capability(
        id="edit-strategy",
        summary=(
            "Turn the search/apply dials: depth, volume, thresholds, autonomy. "
            "The CLI has no settings command at all."
        ),
        api_route="PUT /api/v1/settings/strategy",
        tui_binding="5:settings",
        web_route="/settings",
    ),
    Capability(
        id="browser-hosts",
        summary="Pair, list, and revoke the browsers that take handoffs.",
        cli_command="browser-host list",
        api_route="GET /api/v1/browser-hosts",
    ),
)


def _surfaces(capability: Capability) -> dict[str, str]:
    """The four addresses, as markdown cells."""
    binding = capability.tui_binding
    return {
        "CLI": f"`{capability.cli_command}`" if capability.cli_command else "—",
        "API": f"`{capability.api_route}`" if capability.api_route else "—",
        "TUI": f"`{binding}`" if binding else "—",
        "Web": f"`{capability.web_route}`" if capability.web_route else "—",
    }


def render_markdown() -> str:
    """The whole of docs/parity.md, generated. Deterministic by construction."""
    complete = [c for c in CAPABILITIES if c.everywhere]
    partial = [c for c in CAPABILITIES if not c.everywhere]
    lines = [
        "# Surface parity",
        "",
        "<!-- Generated by `make parity` from applyuminati.surface_parity. Do not edit. -->",
        "",
        "What each of the four surfaces — CLI, API, TUI, web — can do, and the "
        "address it does it at. `—` means the surface cannot do it at all, "
        "which is a product finding, not a missing row.",
        "",
        "| Capability | Summary | CLI | API | TUI | Web |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for capability in CAPABILITIES:
        cells = _surfaces(capability)
        lines.append(
            f"| `{capability.id}` | {capability.summary} | "
            f"{cells['CLI']} | {cells['API']} | {cells['TUI']} | {cells['Web']} |"
        )
    lines += [
        "",
        "## Coverage",
        "",
        f"{len(complete)} of {len(CAPABILITIES)} capabilities are reachable on all "
        "four surfaces: " + ", ".join(f"`{c.id}`" for c in complete) + ".",
        "",
        f"{len(partial)} are missing on at least one surface:",
        "",
    ]
    for capability in partial:
        missing = [
            name
            for name, value in (
                ("CLI", capability.cli_command),
                ("API", capability.api_route),
                ("TUI", capability.tui_binding),
                ("Web", capability.web_route),
            )
            if value is None
        ]
        lines.append(f"- `{capability.id}` — no " + ", ".join(missing))
    lines.append("")
    return "\n".join(lines)
