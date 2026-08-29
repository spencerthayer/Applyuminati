"""Alembic environment.

Runs migrations synchronously against whatever the typed settings resolve to.
``render_as_batch`` is enabled because SQLite cannot ``ALTER TABLE ... DROP
COLUMN`` natively; batch mode rewrites the table instead, which is also safe
on PostgreSQL.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from applyuminati.core.settings import get_settings
from applyuminati.db.base import Base
from applyuminati.db.session import sync_url

import applyuminati.db.models  # noqa: F401 - registers every table on Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The URL comes from application settings, never from alembic.ini.
resolved = sync_url(get_settings().resolved_database_url)
config.set_main_option("sqlalchemy.url", resolved)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without connecting, for review or DBA handoff."""
    context.configure(
        url=resolved,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
