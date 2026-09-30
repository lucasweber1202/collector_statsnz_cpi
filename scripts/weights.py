"""Idempotent, vintage-preserving persistence of target aggregation weights.

Predictor collectors store only published levels. The vintage rules are the
fleet's (GUIDELINES.md 3):

* first sight of ``(series_id, reference_date)`` -- INSERT with
  ``vintage_date`` = the collection date, never the reference date;
* unchanged weight on a later run -- no-op, ``collected_at`` is not touched;
* changed weight, latest stored vintage earlier than today -- INSERT a new
  vintage row;
* changed weight, latest stored vintage IS today -- UPDATE that row in place.

The last rule is what keeps ``(series_id, reference_date, vintage_date)``
unique: a DATE vintage cannot hold two same-day revisions as separate rows, so
the latest collection of the day wins. Databricks does not enforce the primary
key, so the application is the only thing preventing duplicates.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import TextClause, bindparam, text
from sqlalchemy.engine import Connection, Engine

from scripts.config import SCHEMA_NAME

logger = logging.getLogger(__name__)
_TABLE = f"{SCHEMA_NAME}.weights"
BATCH_SIZE = 500
SERIES_BATCH_SIZE = 50
ROUND_DECIMALS = 10
_COLUMNS = ("series_id", "reference_date", "vintage_date", "weight", "collected_at")
_MERGE_DIALECTS = frozenset({"databricks"})


@dataclass(frozen=True)
class WeightObservation:
    series_id: str
    reference_date: date
    weight: float
    snapshot_id: str


@dataclass(frozen=True)
class WeightWriteResult:
    new_weights: int
    new_vintages: int
    written_keys: list[tuple[str, date, date]]
    revised_keys: frozenset[tuple[str, date, date]]
    preexisting_series: frozenset[str]
    same_day_updates: int = 0
    same_day_keys: frozenset[tuple[str, date, date]] = frozenset()


_AGGREGATES_SQL = text(
    f"""SELECT series_id, MIN(reference_date) AS first_weight,
MAX(reference_date) AS last_weight, COUNT(DISTINCT reference_date) AS weight_count,
MAX(collected_at) AS last_collected_at FROM {_TABLE} GROUP BY series_id"""
)
_LATEST_SQL = text(
    f"""SELECT series_id, reference_date, vintage_date, weight, collected_at
FROM (SELECT series_id, reference_date, vintage_date, weight, collected_at,
ROW_NUMBER() OVER (PARTITION BY series_id, reference_date ORDER BY vintage_date DESC,
collected_at DESC) AS rn FROM {_TABLE} WHERE series_id IN :series_ids) ranked WHERE rn = 1"""
).bindparams(bindparam("series_ids", expanding=True))
_MAX_REFERENCE_SQL = text(
    f"SELECT series_id, MAX(reference_date) AS last_weight FROM {_TABLE} GROUP BY series_id"
)
_UPDATE_SQL = text(
    f"""UPDATE {_TABLE} SET weight = :weight, collected_at = :collected_at
WHERE series_id = :series_id AND reference_date = :reference_date
AND vintage_date = :vintage_date"""
)


def _as_date(weight: object) -> date:
    if isinstance(weight, datetime):
        return weight.date()
    if isinstance(weight, str):
        return date.fromisoformat(weight[:10])
    assert isinstance(weight, date)
    return weight


def get_last_weights(engine: Engine) -> dict[str, date]:
    with engine.connect() as conn:
        rows = conn.execute(_MAX_REFERENCE_SQL).mappings().all()
    return {str(row["series_id"]): _as_date(row["last_weight"]) for row in rows}


def get_series_aggregates(conn: Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(_AGGREGATES_SQL).mappings().all()
    return {str(row["series_id"]): dict(row) for row in rows}


def _latest(conn: Connection, series_ids: list[str]) -> dict[tuple[str, date], dict[str, Any]]:
    rows = conn.execute(_LATEST_SQL, {"series_ids": series_ids}).mappings().all()
    return {(str(row["series_id"]), _as_date(row["reference_date"])): dict(row) for row in rows}


def _insert_statement(count: int) -> TextClause:
    weights = ", ".join(
        "(" + ", ".join(f":{column}_{index}" for column in _COLUMNS) + ")" for index in range(count)
    )
    return text(f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) VALUES {weights}")


# A MERGE's source rows are bare parameters in a SELECT, so unlike an INSERT
# there is no target column for the database to infer their type from. When the
# weight is NULL, PostgreSQL types the parameter as `text` and then refuses to
# assign it to a date, numeric or timestamp column. SQLite does not care, which
# is why a SQLite-only test run never sees it and the failure lands on the first
# MERGE against a real warehouse.
#
# Casting in the source fixes it for every weight, NULL included, where binding a
# parameter type does not: the driver still sends an untyped NULL. These type
# names are spelled identically in PostgreSQL and Spark SQL, and this statement
# only runs on those two -- SQLite takes the plain UPDATE path.
#
# Only nullable and date/time columns are listed. The numeric weight columns are
# NOT NULL, so the database always has a real number to infer from, and the two
# dialects do not even agree on the spelling: PostgreSQL rejects DOUBLE and
# Databricks rejects DOUBLE PRECISION. See scripts/init_db.py double_type().
_MERGE_SOURCE_CASTS = {
    "reference_date": "DATE",
    "vintage_date": "DATE",
    "collected_at": "TIMESTAMP",
}


def _merge_source_column(column: str, index: int) -> str:
    """Render one MERGE source column, typed where the column is not a string."""
    parameter = f":{column}_{index}"
    cast = _MERGE_SOURCE_CASTS.get(column)
    expression = f"CAST({parameter} AS {cast})" if cast else parameter
    return f"{expression} AS {column}"


def _merge_statement(count: int) -> TextClause:
    """Batched same-day UPDATE for engines that support MERGE.

    Databricks rejects column aliases on a VALUES clause inside MERGE
    (COLUMN_ALIASES_NOT_ALLOWED), so the source is a UNION ALL of SELECT
    literals, matching the metadata helper.
    """
    source = " UNION ALL ".join(
        "SELECT " + ", ".join(_merge_source_column(column, index) for column in _COLUMNS)
        for index in range(count)
    )
    return text(
        f"MERGE INTO {_TABLE} AS target USING ({source}) AS source "
        "ON target.series_id = source.series_id "
        "AND target.reference_date = source.reference_date "
        "AND target.vintage_date = source.vintage_date "
        # The assigned column is never alias-qualified. Spark SQL tolerates
        # `target.weight`, but PostgreSQL rejects it -- MERGE resolves the SET
        # target against the table itself -- so a same-day revision failed
        # there. Unqualified is correct on both.
        "WHEN MATCHED THEN UPDATE SET weight = source.weight, "
        "collected_at = source.collected_at"
    )


def _batch_parameters(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        f"{column}_{index}": (
            row[column].astimezone(UTC).replace(tzinfo=None)
            if isinstance(row[column], datetime) and row[column].tzinfo
            else row[column]
        )
        for index, row in enumerate(rows)
        for column in _COLUMNS
    }


def _write_inserts(conn: Connection, rows: list[dict[str, Any]]) -> None:
    """Multi-row VALUES inserts with per-batch progress logging."""
    if not rows:
        return
    total = len(rows)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    logger.info("Inserting %d weight rows in %d batches of %d", total, total_batches, BATCH_SIZE)
    written = 0
    for index, start in enumerate(range(0, total, BATCH_SIZE), start=1):
        batch = rows[start : start + BATCH_SIZE]
        conn.execute(_insert_statement(len(batch)), _batch_parameters(batch))
        written += len(batch)
        logger.info("Inserted batch %d/%d (%d/%d rows)", index, total_batches, written, total)


def _write_same_day_updates(conn: Connection, rows: list[dict[str, Any]]) -> None:
    """Overwrite today's vintage in place, batched, with progress logging."""
    if not rows:
        return
    total = len(rows)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    logger.info(
        "Updating %d same-day weight rows in %d batches of %d",
        total,
        total_batches,
        BATCH_SIZE,
    )
    written = 0
    use_merge = conn.dialect.name in _MERGE_DIALECTS
    for index, start in enumerate(range(0, total, BATCH_SIZE), start=1):
        batch = rows[start : start + BATCH_SIZE]
        if use_merge:
            conn.execute(_merge_statement(len(batch)), _batch_parameters(batch))
        else:
            conn.execute(_UPDATE_SQL, batch)
        written += len(batch)
        logger.info("Updated batch %d/%d (%d/%d rows)", index, total_batches, written, total)


def upsert_weights(
    conn: Connection, weights: list[WeightObservation], collected_at: datetime
) -> WeightWriteResult:
    """Apply the fleet vintage rules to this run's weights."""
    collected_at = (
        collected_at.astimezone(UTC).replace(tzinfo=None) if collected_at.tzinfo else collected_at
    )
    today = collected_at.date()
    # Pair each weight with its validated weight: a null or non-finite
    # reading never reaches the table, and the pairing keeps that guarantee
    # visible to the type checker instead of re-asserting it later.
    incoming: list[tuple[WeightObservation, float]] = [
        (o, float(o.weight)) for o in weights if o.weight is not None and math.isfinite(o.weight)
    ]
    if not incoming:
        return WeightWriteResult(0, 0, [], frozenset(), frozenset())
    by_series: dict[str, list[tuple[WeightObservation, float]]] = {}
    for item, value in incoming:
        by_series.setdefault(item.series_id, []).append((item, value))

    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    written_keys: list[tuple[str, date, date]] = []
    revised_keys: set[tuple[str, date, date]] = set()
    same_day_keys: set[tuple[str, date, date]] = set()
    preexisting: set[str] = set()
    new_weights = 0
    new_vintages = 0

    series_ids = sorted(by_series)
    for start in range(0, len(series_ids), SERIES_BATCH_SIZE):
        batch_ids = series_ids[start : start + SERIES_BATCH_SIZE]
        existing = _latest(conn, batch_ids)
        preexisting.update(series_id for series_id, _ in existing)
        for series_id in batch_ids:
            for item, value in by_series[series_id]:
                key = (series_id, item.reference_date)
                current = existing.get(key)
                row = {
                    "series_id": series_id,
                    "reference_date": item.reference_date,
                    "vintage_date": today,
                    "weight": value,
                    "collected_at": collected_at,
                }
                if current is None:
                    inserts.append(row)
                    written_keys.append((series_id, item.reference_date, today))
                    new_weights += 1
                    continue
                unchanged = round(float(current["weight"]), ROUND_DECIMALS) == round(
                    value, ROUND_DECIMALS
                )
                if unchanged:
                    continue
                previous_vintage = _as_date(current["vintage_date"])
                revision_key = (series_id, item.reference_date, today)
                if previous_vintage == today:
                    # A DATE vintage cannot hold two same-day revisions as
                    # separate rows, so the latest collection of the day wins
                    # and replaces today's row in place.
                    updates.append(row)
                    written_keys.append(revision_key)
                    same_day_keys.add(revision_key)
                    continue
                inserts.append(row)
                written_keys.append(revision_key)
                revised_keys.add(revision_key)
                new_vintages += 1

    _write_inserts(conn, inserts)
    _write_same_day_updates(conn, updates)
    logger.info(
        "Weights upsert: new=%d new_vintages=%d same_day_updates=%d",
        new_weights,
        new_vintages,
        len(updates),
    )
    return WeightWriteResult(
        new_weights,
        new_vintages,
        written_keys,
        frozenset(revised_keys),
        frozenset(preexisting),
        len(updates),
        frozenset(same_day_keys),
    )
