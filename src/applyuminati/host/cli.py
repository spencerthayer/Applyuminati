"""CLI for the native Browser Host companion."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import typer

from applyuminati import __version__
from applyuminati.core.settings import get_settings
from applyuminati.host.client import HostClient, open_local_session
from applyuminati.host.discovery import advertise_backends, loopback_url
from applyuminati.host.dispatcher import CommandDispatcher, HostSession
from applyuminati.host.security import require_secure_server

app = typer.Typer(
    name="applyuminati-browser-host",
    help="Native companion that drives a local browser for Applyuminati.",
    no_args_is_help=True,
)


@app.command("run")
def run_host(
    server: str = typer.Option(loopback_url(), "--server", help="Applyuminati WebSocket URL."),
    host_id: str = typer.Option(..., "--host-id", help="Paired host id."),
    credential: str | None = typer.Option(
        None, "--credential", envvar="APPLYUMINATI_HOST_CREDENTIAL", help="Pairing secret."
    ),
    documents_dir: Path | None = typer.Option(
        None, "--documents-dir", help="Directory uploads may be read from."
    ),
    allow_insecure: bool = typer.Option(
        False, "--allow-insecure", help="Allow ws:// to a non-loopback host."
    ),
) -> None:
    """Connect out to Applyuminati and wait for semantic browser commands."""
    if not credential:
        typer.echo("APPLYUMINATI_HOST_CREDENTIAL is required.", err=True)
        raise typer.Exit(code=1)
    require_secure_server(server, allow_insecure=allow_insecure)
    settings = get_settings()
    docs = (documents_dir or (settings.data_dir / "documents")).resolve()
    docs.mkdir(parents=True, exist_ok=True)

    async def _main() -> None:
        backends = await advertise_backends(settings)
        capabilities = frozenset(
            cap
            for advertisement in backends.values()
            if advertisement.available
            for cap in advertisement.capabilities
        )
        dispatcher = CommandDispatcher(documents_dir=docs, capabilities=capabilities)

        async def factory(*, backend: str | None = None) -> HostSession:
            return await open_local_session(settings, backend=backend)

        client = HostClient(
            server=server,
            host_id=host_id,
            credential=credential,
            dispatcher=dispatcher,
            backends=backends,
            allow_insecure=allow_insecure,
            session_factory=factory,
            host_version=__version__,
        )
        await client.run_forever()

    asyncio.run(_main())


@app.command("probe")
def probe() -> None:
    """Show which local browser backends this machine can run."""

    async def _probe() -> None:
        backends = await advertise_backends(get_settings())
        for slug, advertisement in backends.items():
            state = "available" if advertisement.available else "unavailable"
            typer.echo(f"{slug}: {state} {advertisement.detail or ''}".rstrip())

    asyncio.run(_probe())


def main() -> None:
    """Entry point used by ``applyuminati-browser-host``."""
    # A default invocation with no subcommand still needs --host-id, so `run`
    # is registered as a command and users call it explicitly.
    if os.environ.get("APPLYUMINATI_HOST_CREDENTIAL") and len(sys.argv) == 1:
        sys.argv.append("run")
    app()


if __name__ == "__main__":
    main()
