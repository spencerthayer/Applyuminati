"""browser hosts

Adds the pairing table for ``applyuminati-browser-host``. Purely additive: no
existing table is touched, so an existing SQLite install upgrades without
rewriting anything and without a data migration.

Revision ID: 030c7ae8dc8d
Revises: 13629bd800f5
Create Date: 2026-08-31 05:16:27.967091
"""

from __future__ import annotations

from collections.abc import Sequence

import applyuminati.db.base
import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "030c7ae8dc8d"
down_revision: str | None = "13629bd800f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "browser_hosts",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("host_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("platform", sa.String(length=32), nullable=True),
        sa.Column("architecture", sa.String(length=32), nullable=True),
        sa.Column("host_version", sa.String(length=32), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("backends", _JSON, nullable=False),
        # Only the hash is stored. The secret is shown once at pairing, so a copy
        # of this database does not let anyone drive the user's browser.
        sa.Column("credential_hash", sa.String(length=64), nullable=True),
        sa.Column("credential_prefix", sa.String(length=16), nullable=True),
        sa.Column(
            "credential_issued_at",
            applyuminati.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("revoked_at", applyuminati.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("paired_at", applyuminati.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", applyuminati.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column(
            "last_connected_at",
            applyuminati.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("active_sessions", _JSON, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_hosts")),
        # A restarted host must find its own record rather than accumulate a new
        # one on every reconnect.
        sa.UniqueConstraint("host_id", name="uq_browser_hosts_host_id"),
    )
    with op.batch_alter_table("browser_hosts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_browser_hosts_state"), ["state"], unique=False)
        batch_op.create_index(
            "ix_browser_hosts_state_seen", ["state", "last_seen_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("browser_hosts", schema=None) as batch_op:
        batch_op.drop_index("ix_browser_hosts_state_seen")
        batch_op.drop_index(batch_op.f("ix_browser_hosts_state"))
    op.drop_table("browser_hosts")
