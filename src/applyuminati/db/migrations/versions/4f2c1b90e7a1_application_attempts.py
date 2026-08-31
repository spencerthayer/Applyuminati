"""application attempts

Adds the execution-level ApplicationAttempt table. Additive: existing
application lifecycle rows are untouched.

Revision ID: 4f2c1b90e7a1
Revises: 030c7ae8dc8d
Create Date: 2026-08-31 06:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import applyuminati.db.base
import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "4f2c1b90e7a1"
down_revision: str | None = "030c7ae8dc8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "application_attempts",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("application_id", sa.String(length=26), nullable=False),
        sa.Column("job_id", sa.String(length=26), nullable=False),
        sa.Column("profile_id", sa.String(length=26), nullable=True),
        sa.Column("driver", sa.String(length=64), nullable=False),
        sa.Column("driver_version", sa.String(length=16), nullable=False),
        sa.Column("workflow_state", sa.String(length=32), nullable=False),
        sa.Column("current_step", sa.String(length=80), nullable=True),
        sa.Column("submission_mode", sa.String(length=32), nullable=False),
        sa.Column("browser_host_id", sa.String(length=128), nullable=True),
        sa.Column("browser_backend", sa.String(length=64), nullable=True),
        sa.Column("browser_session_id", sa.String(length=64), nullable=True),
        sa.Column("task_space_id", sa.String(length=128), nullable=True),
        sa.Column("task_space_numeric_id", sa.Integer(), nullable=True),
        sa.Column("started_at", applyuminati.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", applyuminati.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("completed_at", applyuminati.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column(
            "submission_attempted_at",
            applyuminati.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("payload", _JSON, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_attempts")),
    )
    with op.batch_alter_table("application_attempts", schema=None) as batch_op:
        batch_op.create_index("ix_attempts_application", ["application_id", "updated_at"])
        batch_op.create_index("ix_attempts_workflow_state", ["workflow_state", "updated_at"])
        batch_op.create_index(batch_op.f("ix_application_attempts_application_id"), ["application_id"])
        batch_op.create_index(batch_op.f("ix_application_attempts_job_id"), ["job_id"])


def downgrade() -> None:
    with op.batch_alter_table("application_attempts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_application_attempts_job_id"))
        batch_op.drop_index(batch_op.f("ix_application_attempts_application_id"))
        batch_op.drop_index("ix_attempts_workflow_state")
        batch_op.drop_index("ix_attempts_application")
    op.drop_table("application_attempts")
