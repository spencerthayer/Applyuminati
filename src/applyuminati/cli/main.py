"""Typer CLI entry point.

Every command calls the same application services as the FastAPI routers —
no CLI-only business logic exists anywhere. Commands that need async services
run them through :func:`asyncio.run` on a fresh event loop.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from applyuminati import __version__
from applyuminati.core.settings import get_settings
from applyuminati.services.container import ServiceContainer, get_container

app = typer.Typer(
    name="applyuminati",
    help="Local-first, autonomous, LLM-powered job search and application platform.",
    no_args_is_help=True,
)
console = Console()


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def _container() -> ServiceContainer:
    return get_container()


# -- init ----------------------------------------------------------------


@app.command()
def init() -> None:
    """Initialise the data directory and configuration."""
    settings = get_settings()
    settings.ensure_directories()
    console.print(f"[green]Initialised Applyuminati at {settings.data_dir}[/green]")
    console.print(f"  Database: {settings.db_path}")
    console.print(f"  Config:   {settings.config_path}")
    console.print(f"  Mode:     {settings.execution_mode.value}")
    console.print("\nNext steps:")
    console.print("  1. Import your resume:  applyuminati profile import resume.json")
    console.print("  2. Enable a source:     applyuminati sources enable greenhouse")
    console.print("  3. Discover jobs:       applyuminati jobs discover")
    console.print("  4. Start the server:    applyuminati serve")


# -- doctor ---------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Check the health of every component."""
    container = _container()

    async def _check() -> None:
        db_ok = await container.database.check()
        schema = await container.database.schema_version()
        async with container.repositories() as repos:
            from applyuminati.services.health_service import HealthService

            svc = HealthService(repos, container.settings, container._llm)
            summary = await svc.summary(db_ok, schema)
            backends = await svc.backends()

        table = Table(title="Applyuminati Doctor")
        table.add_column("Component")
        table.add_column("Status")
        table.add_column("Detail")
        table.add_row("Database", "ok" if db_ok else "FAIL", str(container.settings.db_path))
        table.add_row("Schema", schema or "not migrated", "")
        table.add_row(
            "Profile", "configured" if summary["profile_configured"] else "not imported", ""
        )
        table.add_row("Mode", summary["execution_mode"], "")
        table.add_row(
            "Sources", ", ".join(summary.get("enabled_sources", [])) or "(none enabled)", ""
        )
        console.print(table)

        bt = Table(title="Backends")
        bt.add_column("Kind")
        bt.add_column("Name")
        bt.add_column("State")
        bt.add_column("Detail")
        for kind, reports in [
            ("source", backends.sources),
            ("llm", backends.llm),
            ("browser", backends.browsers),
            ("agent", backends.agents),
            ("email", backends.email),
        ]:
            for report in reports:
                bt.add_row(kind, report.plugin, report.state.value, report.detail[:60])
        console.print(bt)
        if backends.load_errors:
            console.print("[red]Load errors:[/red]")
            for err in backends.load_errors:
                console.print(f"  {err}")

    _run_async(_check())


# -- serve ----------------------------------------------------------------


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8000, help="Port"),
    reload: bool = typer.Option(False, help="Auto-reload on file changes"),
) -> None:
    """Start the local API server."""
    import uvicorn

    console.print(f"[green]Starting Applyuminati on http://{host}:{port}[/green]")
    uvicorn.run(
        "applyuminati.api.app:app",
        host=host,
        port=port,
        reload=reload,
    )


# -- auth -----------------------------------------------------------------


auth_app = typer.Typer(help="Manage access to the local API and UI.")
app.add_typer(auth_app, name="auth")


@auth_app.command("hash-password")
def auth_hash_password(
    password: str = typer.Option(
        ...,
        prompt=True,
        confirmation_prompt=True,
        hide_input=True,
        help="Read from a prompt so it never lands in shell history.",
    ),
) -> None:
    """Print a PBKDF2 hash to use as the configured password.

    Use this instead of a plaintext password wherever other people can read the
    process environment or the compose file: a shared host, a NAS, a machine with
    other users. The hash verifies logins and cannot be replayed as the password.
    """
    from applyuminati.core.security import WeakPasswordError, hash_password

    try:
        encoded = hash_password(password)
    except WeakPasswordError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print("\n[green]Add this to config.toml:[/green]")
    console.print(f'\n[security]\npassword = "{encoded}"\n')
    console.print("[green]Or export it:[/green]")
    console.print(f"\nAPPLYUMINATI_SECURITY__PASSWORD='{encoded}'\n")


@auth_app.command("status")
def auth_status() -> None:
    """Report whether the API is protected, and how far it is reachable."""
    settings = get_settings()
    security = settings.security
    table = Table(title="Access control")
    table.add_column("Property")
    table.add_column("Value")
    table.add_row("Authentication", "enabled" if security.enabled else "DISABLED")
    table.add_row("Password", "set" if security.configured_secret() else "NOT SET")
    table.add_row("Bind address", settings.server.host)
    table.add_row(
        "Reachable from other machines",
        "yes" if settings.listens_beyond_loopback else "no (loopback only)",
    )
    table.add_row("Secure cookies (HTTPS)", "yes" if security.https_only else "no")
    table.add_row("Session lifetime", f"{security.session_ttl_hours}h")
    console.print(table)
    if security.enabled and not security.configured_secret():
        console.print(
            "[yellow]The API will refuse every request except health and login "
            "until a password is set. Run: applyuminati auth hash-password[/yellow]"
        )


# -- profile --------------------------------------------------------------


profile_app = typer.Typer(help="Manage the canonical career profile.")
app.add_typer(profile_app, name="profile")


@profile_app.command("import")
def profile_import(
    path: Path = typer.Argument(..., help="Path to a JSON Resume file"),
    replace: bool = typer.Option(False, "--replace", help="Overwrite an existing profile"),
) -> None:
    """Import a JSON Resume file."""
    container = _container()

    async def _import() -> None:
        async with container.repositories() as repos:
            from applyuminati.services.profile_service import ProfileService

            svc = ProfileService(repos)
            result = await svc.import_from_path(path, replace=replace)
        console.print(f"[green]Imported profile: {result.profile.name}[/green]")
        console.print(f"  Claims:   {result.claims_created}")
        console.print(f"  Metrics:  {result.metrics_extracted}")
        if result.warnings:
            console.print("[yellow]Warnings:[/yellow]")
            for warning in result.warnings:
                console.print(f"  {warning}")

    _run_async(_import())


@profile_app.command("export")
def profile_export(
    path: Path = typer.Argument(..., help="Output path for the JSON Resume file"),
) -> None:
    """Export the current profile as a JSON Resume file."""
    container = _container()

    async def _export() -> None:
        async with container.repositories() as repos:
            from applyuminati.services.profile_service import ProfileService

            svc = ProfileService(repos)
            resume = await svc.export_resume()
        path.write_text(json.dumps(resume, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"[green]Exported to {path}[/green]")

    _run_async(_export())


# -- sources --------------------------------------------------------------


sources_app = typer.Typer(help="Manage job sources.")
app.add_typer(sources_app, name="sources")


@sources_app.command("list")
def sources_list() -> None:
    """List all registered job sources."""
    container = _container()

    async def _list() -> None:
        async with container.repositories() as repos:
            from applyuminati.services.source_service import SourceService

            svc = SourceService(repos, container.settings)
            views = await svc.list()
        table = Table(title="Job Sources")
        table.add_column("Slug")
        table.add_column("Name")
        table.add_column("Tier")
        table.add_column("Enabled")
        table.add_column("Health")
        for view in views:
            table.add_row(
                view.slug,
                view.name,
                view.tier,
                "✓" if view.enabled else "",
                view.health.state.value if view.health else "unknown",
            )
        console.print(table)

    _run_async(_list())


@sources_app.command("enable")
def sources_enable(
    slug: str = typer.Argument(..., help="Source slug, e.g. greenhouse"),
) -> None:
    """Enable a job source."""
    container = _container()

    async def _enable() -> None:
        async with container.repositories() as repos:
            from applyuminati.services.source_service import SourceService

            svc = SourceService(repos, container.settings)
            view = await svc.set_enabled(slug, True)
        console.print(f"[green]Enabled source: {view.name}[/green]")

    _run_async(_enable())


@sources_app.command("disable")
def sources_disable(
    slug: str = typer.Argument(..., help="Source slug"),
) -> None:
    """Disable a job source."""
    container = _container()

    async def _disable() -> None:
        async with container.repositories() as repos:
            from applyuminati.services.source_service import SourceService

            svc = SourceService(repos, container.settings)
            view = await svc.set_enabled(slug, False)
        console.print(f"[green]Disabled source: {view.name}[/green]")

    _run_async(_disable())


# -- jobs -----------------------------------------------------------------


jobs_app = typer.Typer(help="Discover, list, and score jobs.")
app.add_typer(jobs_app, name="jobs")


@jobs_app.command("discover")
def jobs_discover(
    source: list[str] = typer.Option(default_factory=list, help="Restrict to these sources"),
    query: list[str] = typer.Option(default_factory=list, help="Search queries"),
) -> None:
    """Run job discovery across enabled sources."""
    container = _container()

    async def _discover() -> None:
        async with container.repositories() as repos:
            from applyuminati.services.discovery_service import DiscoveryService

            svc = DiscoveryService(repos, container.settings)
            run = await svc.discover(
                sources=source or None, queries=query or None, triggered_by="cli"
            )
        console.print(f"[green]Discovery complete: {run.state.value}[/green]")
        console.print(f"  Discovered: {run.stats.get('jobs_discovered', 0)}")
        console.print(f"  Created:    {run.stats.get('jobs_created', 0)}")
        console.print(f"  Merged:     {run.stats.get('jobs_merged', 0)}")
        if run.failures:
            console.print("[red]Failures:[/red]")
            for failure in run.failures:
                console.print(f"  {failure}")

    _run_async(_discover())


@jobs_app.command("list")
def jobs_list(
    limit: int = typer.Option(20, help="Maximum results"),
    source: str = typer.Option(None, help="Filter by source"),
) -> None:
    """List discovered jobs."""
    container = _container()

    async def _list() -> None:
        async with container.repositories() as repos:
            from applyuminati.services.job_service import JobService

            svc = JobService(repos)
            page = await svc.list(sources=[source] if source else None, limit=limit)
        table = Table(title=f"Jobs ({page.total} total)")
        table.add_column("Title")
        table.add_column("Company")
        table.add_column("Source(s)")
        table.add_column("Score")
        table.add_column("Rec.")
        table.add_column("State")
        for view in page.items:
            table.add_row(
                view.job.title[:40],
                view.job.company[:20],
                ", ".join(view.job.source_slugs),
                f"{view.score.overall:.2f}" if view.score else "—",
                view.score.recommendation.value if view.score else "—",
                view.application_state.value if view.application_state else "—",
            )
        console.print(table)

    _run_async(_list())


@jobs_app.command("score")
def jobs_score(
    use_llm: bool = typer.Option(False, "--llm", help="Use LLM enrichment"),
    limit: int = typer.Option(100, help="Maximum jobs to score"),
) -> None:
    """Score unscored jobs against the profile."""
    container = _container()

    async def _score() -> None:
        async with container.repositories() as repos:
            from applyuminati.services.scoring_service import ScoringService

            svc = ScoringService(repos, container.settings, container.llm)
            run = await svc.score_jobs(use_llm=use_llm, limit=limit, triggered_by="cli")
        console.print(f"[green]Scoring complete: {run.state.value}[/green]")
        console.print(f"  Scored:  {run.stats.get('scored', 0)}")
        console.print(f"  Failed: {run.stats.get('failed', 0)}")
        if run.failures:
            console.print("[red]Failures:[/red]")
            for failure in run.failures:
                console.print(f"  {failure}")

    _run_async(_score())


# -- status ---------------------------------------------------------------


@app.command()
def status() -> None:
    """Show the current application pipeline status."""
    container = _container()

    async def _status() -> None:
        async with container.repositories() as repos:
            from applyuminati.services.dashboard_service import DashboardService

            svc = DashboardService(repos)
            view = await svc.build()
        console.print(f"[bold]Applyuminati Status[/bold] (v{__version__})")
        console.print(f"  Total jobs:       {view.total_jobs}")
        console.print(f"  Shortlisted:      {view.shortlisted}")
        console.print(f"  Ready:            {view.ready}")
        console.print(f"  Submitted:        {view.submitted}")
        console.print(f"  Needs attention:  {view.needs_attention}")
        console.print(f"  Scored:           {view.scored} / {view.total_jobs}")
        if view.by_recommendation:
            console.print("\n  By recommendation:")
            for rec, count in sorted(view.by_recommendation.items()):
                console.print(f"    {rec}: {count}")
        if view.latest_run:
            run = view.latest_run
            console.print(f"\n  Latest run: {run.kind} ({run.state.value})")
            console.print(f"    Stats: {run.stats}")

    _run_async(_status())
