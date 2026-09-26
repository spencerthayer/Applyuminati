"""A new install must reach a working state with one command.

`init` used to create the data directory, print that it had created a
config.toml, and stop. It did not migrate, so the very next command died on
`no such table: profiles`, and it never provisioned a password, so the API
answered 503 on every route.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applyuminati.cli.main import app
from applyuminati.core.logging import configure_logging
from applyuminati.core.settings import LogFormat, Settings, set_settings
from applyuminati.db.session import set_database
from applyuminati.services.container import get_container, set_container

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_process_singletons():
    """The CLI caches settings, the database, and the container process-wide.

    That is correct for a real command, which is one command per process, but
    CliRunner invokes several in one process, so the first test's data dir would
    otherwise be served to every test after it.
    """
    set_settings(None)
    set_database(None)
    set_container(None)
    yield
    # Dispose before clearing. A command like doctor builds a real container on
    # a real data directory, and an undisposed engine leaves a daemon
    # aiosqlite thread that throws "Event loop is closed" during whichever
    # later test happens to be running.
    container = get_container()
    if container is not None:
        asyncio.run(container.aclose())
    set_settings(None)
    set_database(None)
    set_container(None)
    # Any command that builds a container calls configure_logging, which rebinds
    # structlog to the Typer runner's captured stdout. The runner closes that on
    # exit, so a later test logging through the leftover handler raises
    # "I/O operation on closed file". Re-point it at the real stream.
    configure_logging(level="INFO", fmt=LogFormat.CONSOLE)


def _run_init(data_dir: Path, *args: str):
    return runner.invoke(app, ["init", *args], env={"APPLYUMINATI_DATA_DIR": str(data_dir)})


def _tables(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_init_creates_the_data_directory(tmp_path: Path) -> None:
    data = tmp_path / "fresh"
    result = _run_init(data)
    assert result.exit_code == 0, result.output
    assert data.is_dir()


def test_init_migrates_the_database(tmp_path: Path) -> None:
    """The next command must not die on a missing table."""
    data = tmp_path / "fresh"
    result = _run_init(data)
    assert result.exit_code == 0, result.output
    tables = _tables(data / "applyuminati.db")
    assert "profiles" in tables
    assert "jobs" in tables
    assert "tasks" in tables
    assert "application_attempts" in tables


def test_init_reports_the_real_revision(tmp_path: Path) -> None:
    """A coroutine repr or a bare 'at head' means the read never happened."""
    data = tmp_path / "fresh"
    result = _run_init(data)
    assert result.exit_code == 0, result.output
    assert "coroutine" not in result.output
    revision = next(
        (ln.split(":", 1)[1].strip() for ln in result.output.splitlines() if "Schema:" in ln),
        "",
    )
    assert re.fullmatch(r"[0-9a-f]{12}", revision), revision


def test_init_writes_a_real_config_file(tmp_path: Path) -> None:
    """It used to print a path to a config.toml it never wrote."""
    data = tmp_path / "fresh"
    result = _run_init(data)
    assert result.exit_code == 0, result.output
    config = data / "config.toml"
    assert config.is_file()
    assert config.stat().st_size > 0


def test_init_does_not_overwrite_an_existing_config(tmp_path: Path) -> None:
    data = tmp_path / "fresh"
    data.mkdir(parents=True)
    (data / "config.toml").write_text("# mine\n[discovery.sources.lever]\nenabled = true\n")
    result = _run_init(data)
    assert result.exit_code == 0, result.output
    assert (data / "config.toml").read_text().startswith("# mine")


def test_init_provisions_a_password(tmp_path: Path) -> None:
    """Without a password every API route answers 503 auth.not_configured."""
    data = tmp_path / "fresh"
    result = _run_init(data)
    assert result.exit_code == 0, result.output
    assert "password" in result.output.lower()

    settings = Settings(data_dir=data)
    assert settings.security.password is not None
    shown = result.output
    assert "Password: generated" in shown


def test_an_explicit_password_is_stored_as_a_verifiable_hash(tmp_path: Path) -> None:
    """The config holds a hash, not the plaintext. The user already has the plaintext."""
    from applyuminati.core.security import verify_configured_password

    data = tmp_path / "fresh"
    result = _run_init(data, "--password", "correct-horse-battery")
    assert result.exit_code == 0, result.output
    stored = Settings(data_dir=data).security.password
    assert stored is not None
    raw = stored.get_secret_value()
    assert "correct-horse-battery" not in raw
    assert verify_configured_password("correct-horse-battery", raw)


def test_init_is_repeatable(tmp_path: Path) -> None:
    data = tmp_path / "fresh"
    assert _run_init(data).exit_code == 0
    second = _run_init(data)
    assert second.exit_code == 0, second.output


def test_init_rejects_a_weak_explicit_password(tmp_path: Path) -> None:
    data = tmp_path / "fresh"
    result = _run_init(data, "--password", "abc")
    assert result.exit_code != 0


def test_doctor_works_immediately_after_init(tmp_path: Path) -> None:
    """The whole point: one command, then the health check works."""
    data = tmp_path / "fresh"
    assert _run_init(data).exit_code == 0
    doctor = runner.invoke(app, ["doctor"], env={"APPLYUMINATI_DATA_DIR": str(data)})
    assert doctor.exit_code == 0, doctor.output
    assert "no such table" not in doctor.output
    assert "not imported" in doctor.output


def test_init_prints_next_steps_that_are_true(tmp_path: Path) -> None:
    """A banner that lies is worse than no banner."""
    data = tmp_path / "fresh"
    result = _run_init(data)
    assert result.exit_code == 0
    lowered = result.output.lower()
    assert "config.toml" in lowered
    # It must not send the user to run migrations by hand; init did it.
    assert "alembic upgrade" not in lowered


@pytest.mark.parametrize("missing", ["config.toml"])
def test_init_creates_every_thing_it_claims(tmp_path: Path, missing: str) -> None:
    data = tmp_path / "fresh"
    assert _run_init(data).exit_code == 0
    assert (data / missing).exists()
