"""Declarative base and portable column types.

Every choice here is made so that swapping SQLite for PostgreSQL later is a
configuration change, not a rewrite:

* ``JSONText`` maps to the dialect's native JSON type; on PostgreSQL it
  becomes ``JSONB`` automatically.
* ``UTCDateTime`` stores timezone-aware UTC and *returns* timezone-aware UTC,
  which SQLite otherwise will not do.
* Identifiers are 26-character ULID strings, not autoincrement integers, so
  rows can be created offline and merged without collision.
* No dialect-specific DDL, no ``sqlite_`` kwargs, no server-side defaults that
  only one engine understands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, String, TypeDecorator
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import JSON

#: Explicit naming convention so Alembic can autogenerate stable constraint
#: names; without this, SQLite batch migrations produce unnamed constraints
#: that cannot be dropped later.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Portable JSON: JSONB on PostgreSQL, JSON elsewhere.
JSONText = JSON().with_variant(postgresql.JSONB(), "postgresql")

#: ULID primary keys.
ULID = String(26)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    SQLite has no native timestamp type and drops tzinfo. This decorator
    normalises on the way in and re-attaches UTC on the way out, so domain
    code never sees a naive datetime.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ANN401
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ANN401
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy API
        dict[str, Any]: JSONText,
        list[str]: JSONText,
        list[Any]: JSONText,
        datetime: UTCDateTime,
    }


__all__ = ["JSONText", "NAMING_CONVENTION", "ULID", "Base", "UTCDateTime"]
