"""Build and idempotently upsert predictor metadata after observation writes.

History fields are read back from `time_series` after the observation write, so
they describe the full stored history rather than the current extraction window.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import TextClause, text
from sqlalchemy.engine import Connection

from scripts.config import COUNTRY_CURRENCY, METADATA_TABLE, SCHEMA_NAME
from scripts.extract import ECO_GROUPS, FREQUENCIES, UNITS
from scripts.time_series import get_series_aggregates

logger = logging.getLogger(__name__)
_TABLE = f"{SCHEMA_NAME}.{METADATA_TABLE}"
BATCH_SIZE = 500


_COMPARABLE_COLUMNS = (
    "name",
    "description",
    "country",
    "frequency",
    "unit",
    "first_observation",
    "last_observation",
    "observation_count",
    "eco_group",
    "source_url",
    "last_publish_date",
)
_INSERT_COLUMNS = ("series_id", *_COMPARABLE_COLUMNS, "collected_at")
_COLUMNS = _INSERT_COLUMNS
_UPDATE_COLUMNS = tuple(column for column in _COLUMNS if column != "series_id")
_MERGE_DIALECTS = frozenset({"databricks", "postgresql"})

_SELECT_SQL = text(f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE}")


def _batch_parameters(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a batch into named parameters suffixed by row position."""
    return {
        f"{column}_{index}": (row[column].astimezone(UTC).replace(tzinfo=None) if isinstance(row[column], datetime) and row[column].tzinfo else row[column]) for index, row in enumerate(rows) for column in _COLUMNS
    }


def _insert_statement(count: int) -> TextClause:
    """Build one multi-row INSERT covering ``count`` rows."""
    values = ", ".join(
        "(" + ", ".join(f":{column}_{index}" for column in _COLUMNS) + ")" for index in range(count)
    )
    return text(f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) VALUES {values}")


# A MERGE's source rows are bare parameters in a SELECT, so unlike an INSERT
# there is no target column for the database to infer their type from. When the
# value is NULL, PostgreSQL types the parameter as `text` and then refuses to
# assign it to a date, numeric or timestamp column. SQLite does not care, which
# is why a SQLite-only test run never sees it and the failure lands on the first
# MERGE against a real warehouse.
#
# Casting in the source fixes it for every value, NULL included, where binding a
# parameter type does not: the driver still sends an untyped NULL. These type
# names are spelled identically in PostgreSQL and Spark SQL, and this statement
# only runs on those two -- SQLite takes the plain UPDATE path.
#
# Only nullable and date/time columns are listed. The numeric value columns are
# NOT NULL, so the database always has a real number to infer from, and the two
# dialects do not even agree on the spelling: PostgreSQL rejects DOUBLE and
# Databricks rejects DOUBLE PRECISION. See scripts/init_db.py double_type().
_MERGE_SOURCE_CASTS = {
    "first_observation": "DATE",
    "last_observation": "DATE",
    "last_publish_date": "DATE",
    "collected_at": "TIMESTAMP",
    "observation_count": "INT",
}


def _merge_source_column(column: str, index: int) -> str:
    """Render one MERGE source column, typed where the column is not a string."""
    parameter = f":{column}_{index}"
    cast = _MERGE_SOURCE_CASTS.get(column)
    expression = f"CAST({parameter} AS {cast})" if cast else parameter
    return f"{expression} AS {column}"


def _merge_statement(count: int) -> TextClause:
    """Build one Databricks-compatible MERGE covering ``count`` rows."""
    source = " UNION ALL ".join(
        "SELECT " + ", ".join(_merge_source_column(column, index) for column in _COLUMNS)
        for index in range(count)
    )
    assignments = ", ".join(f"{column} = source.{column}" for column in _UPDATE_COLUMNS)
    return text(
        f"MERGE INTO {_TABLE} AS target USING ({source}) AS source "
        "ON target.series_id = source.series_id "
        f"WHEN MATCHED THEN UPDATE SET {assignments}"
    )


_UPDATE_SQL = text(
    f"UPDATE {_TABLE} SET {', '.join(f'{column}=:{column}' for column in _UPDATE_COLUMNS)} "
    "WHERE series_id=:series_id"
)


def _as_date(value: object) -> date | None:
    """Coerce a driver-returned date/datetime/string to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    assert isinstance(value, date)
    return value


def validate_catalog(catalog: dict[str, dict[str, Any]]) -> None:
    """Refuse a descriptor that uses a value outside the declared vocabulary.

    Emitting an unvocabularied spelling is how a fleet vocabulary quietly forks,
    so this is a hard failure rather than a warning.
    """
    for series_id, fields in sorted(catalog.items()):
        for key in ("name", "source_url"):
            if not str(fields.get(key, "")).strip():
                raise ValueError(f"{series_id} metadata is missing required field {key!r}")
        if fields["frequency"] not in FREQUENCIES:
            raise ValueError(f"{series_id} has unknown frequency {fields['frequency']!r}")
        if fields["unit"] not in UNITS:
            raise ValueError(f"{series_id} has unknown unit {fields['unit']!r}")
        if fields["eco_group"] not in ECO_GROUPS:
            raise ValueError(f"{series_id} has unknown eco_group {fields['eco_group']!r}")
        if fields.get("country", COUNTRY_CURRENCY) != COUNTRY_CURRENCY:
            raise ValueError(f"{series_id} must carry country {COUNTRY_CURRENCY}")


def upsert_metadata(
    conn: Connection,
    catalog: dict[str, dict[str, Any]],
    collected_at: datetime,
) -> tuple[int, int]:
    """Upsert one row per catalogued series present in the database.

    Returns ``(inserted, updated)``. An unchanged row is neither, so a rerun
    against an unchanged source writes nothing here, including `collected_at`.
    """
    collected_at = collected_at.astimezone(UTC).replace(tzinfo=None) if collected_at.tzinfo else collected_at
    validate_catalog(catalog)
    aggregates = get_series_aggregates(conn)
    existing = {
        str(row["series_id"]): dict(row) for row in conn.execute(_SELECT_SQL).mappings().all()
    }

    desired: list[dict[str, Any]] = []
    for series_id, fields in sorted(catalog.items()):
        history = aggregates.get(series_id)
        if history is None:
            # A catalogued series with no stored observation is not described:
            # metadata must never claim a series the database does not hold.
            logger.warning("%s has no stored observations; skipping metadata", series_id)
            continue
        desired.append(
            {
                "series_id": series_id,
                "name": fields["name"],
                "description": fields["description"],
                "country": COUNTRY_CURRENCY,
                "frequency": fields["frequency"],
                "unit": fields["unit"],
                "first_observation": _as_date(history["first_observation"]),
                "last_observation": _as_date(history["last_observation"]),
                "observation_count": int(history["observation_count"]),
                "eco_group": fields["eco_group"],
                "source_url": fields["source_url"],
                "last_publish_date": _as_date(fields.get("last_publish_date"))
                or _as_date(history["last_collected_at"]),
                "collected_at": collected_at,
            }
        )

    inserts = [row for row in desired if row["series_id"] not in existing]
    updates = []
    for row in desired:
        current = existing.get(row["series_id"])
        if current is None:
            continue
        changed = any(
            _normalize(row[column]) != _normalize(current.get(column))
            for column in _COMPARABLE_COLUMNS
        )
        if changed:
            updates.append(row)

    merge = conn.dialect.name in _MERGE_DIALECTS
    if inserts:
        total_batches = (len(inserts) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info(
            "Inserting %d rows in %d batches of %d", len(inserts), total_batches, BATCH_SIZE
        )
        inserted_rows = 0
        for batch_index, start in enumerate(range(0, len(inserts), BATCH_SIZE), start=1):
            batch = inserts[start : start + BATCH_SIZE]
            conn.execute(_insert_statement(len(batch)), _batch_parameters(batch))
            inserted_rows += len(batch)
            logger.info(
                "Inserted batch %d/%d (%d/%d rows)",
                batch_index,
                total_batches,
                inserted_rows,
                len(inserts),
            )
    if updates:
        total_batches = (len(updates) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info("Updating %d rows in %d batches of %d", len(updates), total_batches, BATCH_SIZE)
        updated_rows = 0
        for batch_index, start in enumerate(range(0, len(updates), BATCH_SIZE), start=1):
            batch = updates[start : start + BATCH_SIZE]
            if merge:
                conn.execute(_merge_statement(len(batch)), _batch_parameters(batch))
            else:
                conn.execute(_UPDATE_SQL, batch)
            updated_rows += len(batch)
            logger.info(
                "Updated batch %d/%d (%d/%d rows)",
                batch_index,
                total_batches,
                updated_rows,
                len(updates),
            )
    logger.info("Metadata upsert: inserted=%d updated=%d", len(inserts), len(updates))
    return len(inserts), len(updates)


def _normalize(value: object) -> object:
    """Normalize a stored value for comparison across driver type mappings."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return str(value)
