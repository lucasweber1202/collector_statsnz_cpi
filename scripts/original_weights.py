"""Persist official Table 8 base expenditure weights and the CPI hierarchy.

``original_weights`` stores each published Table 8 cell unmodified, in percent
of all-groups expenditure, keyed like ``time_series``:
``(series_id, reference_date, vintage_date)``. ``reference_date`` is the base
(price-reference) quarter end the weight is expressed in, never a date the
weight was applied to. The vintage rules are the fleet's time-series rules:
first sight inserts today's vintage, an unchanged figure is a no-op, a changed
figure on a later day adds a vintage, and a same-day change overwrites today's
row. The all-groups row (100 by definition) is not published in Table 8 and is
therefore not stored.

These are **base** weights. They are not effective quarterly aggregation
weights: the share a component carries in a later quarter is its base share
price-updated by its own index relative, which consumers derive; it is not a
published figure and is not stored here.

``cpi_hierarchy`` is a plain dimension table keyed by ``series_id``: native
code, level and parent, derived from the published codes and checked against
the published group labels. It carries no vintage because it describes what an
identifier means; a relabelled code is corrected in place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import TextClause, bindparam, text
from sqlalchemy.engine import Connection

from scripts.config import HIERARCHY_TABLE, ORIGINAL_WEIGHTS_TABLE, SCHEMA_NAME
from scripts.weight_sources import BaseWeight, hierarchy_level, parent_id

logger = logging.getLogger(__name__)
_WEIGHTS = f"{SCHEMA_NAME}.{ORIGINAL_WEIGHTS_TABLE}"
_HIERARCHY = f"{SCHEMA_NAME}.{HIERARCHY_TABLE}"
BATCH_SIZE = 500
ROUND_DECIMALS = 10
PREFIX = "STATSNZ_CPI_CPIQ_"
_WEIGHT_COLUMNS = (
    "series_id",
    "reference_date",
    "vintage_date",
    "weight",
    "weight_base_year",
    "collected_at",
)
_HIERARCHY_COLUMNS = ("series_id", "native_code", "parent_id", "level", "name", "collected_at")
_HIERARCHY_COMPARABLE = ("native_code", "parent_id", "level", "name")
_WEIGHT_MERGE_DIALECTS = frozenset({"databricks"})
_HIERARCHY_MERGE_DIALECTS = frozenset({"databricks", "postgresql"})

_LATEST_WEIGHTS_SQL = text(
    f"""SELECT series_id, reference_date, vintage_date, weight
FROM (SELECT series_id, reference_date, vintage_date, weight,
ROW_NUMBER() OVER (PARTITION BY series_id, reference_date ORDER BY vintage_date DESC,
collected_at DESC) AS rn FROM {_WEIGHTS} WHERE series_id IN :series_ids) ranked WHERE rn = 1"""
).bindparams(bindparam("series_ids", expanding=True))
_UPDATE_WEIGHT_SQL = text(
    f"""UPDATE {_WEIGHTS} SET weight = :weight, collected_at = :collected_at
WHERE series_id = :series_id AND reference_date = :reference_date
AND vintage_date = :vintage_date"""
)
_SELECT_HIERARCHY_SQL = text(f"SELECT {', '.join(_HIERARCHY_COLUMNS)} FROM {_HIERARCHY}")
_UPDATE_HIERARCHY_SQL = text(
    f"UPDATE {_HIERARCHY} SET "
    + ", ".join(f"{column} = :{column}" for column in _HIERARCHY_COLUMNS if column != "series_id")
    + " WHERE series_id = :series_id"
)

# See scripts/metadata.py: MERGE source parameters are untyped, so PostgreSQL
# needs the nullable and date/time columns cast for a NULL to be assignable.
_WEIGHT_CASTS = {
    "reference_date": "DATE",
    "vintage_date": "DATE",
    "collected_at": "TIMESTAMP",
    "weight_base_year": "INT",
}
_HIERARCHY_CASTS = {"parent_id": "VARCHAR(200)", "level": "INT", "collected_at": "TIMESTAMP"}


@dataclass(frozen=True)
class HierarchyNode:
    series_id: str
    native_code: str
    parent_id: str | None
    level: int
    name: str


@dataclass(frozen=True)
class WeightWriteResult:
    new_weights: int
    new_vintages: int
    same_day_updates: int


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    assert isinstance(value, date)
    return value


def _source_column(column: str, index: int, casts: dict[str, str]) -> str:
    parameter = f":{column}_{index}"
    cast = casts.get(column)
    return f"{f'CAST({parameter} AS {cast})' if cast else parameter} AS {column}"


def _parameters(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> dict[str, Any]:
    return {
        f"{column}_{index}": row[column] for index, row in enumerate(rows) for column in columns
    }


def _insert_statement(table: str, columns: tuple[str, ...], count: int) -> TextClause:
    values = ", ".join(
        "(" + ", ".join(f":{column}_{index}" for column in columns) + ")" for index in range(count)
    )
    return text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {values}")


def weight_merge_statement(count: int) -> TextClause:
    """Batched same-day weight UPDATE for engines that support MERGE."""
    source = " UNION ALL ".join(
        "SELECT " + ", ".join(_source_column(c, i, _WEIGHT_CASTS) for c in _WEIGHT_COLUMNS)
        for i in range(count)
    )
    return text(
        f"MERGE INTO {_WEIGHTS} AS target USING ({source}) AS source "
        "ON target.series_id = source.series_id "
        "AND target.reference_date = source.reference_date "
        "AND target.vintage_date = source.vintage_date "
        "WHEN MATCHED THEN UPDATE SET weight = source.weight, collected_at = source.collected_at"
    )


def hierarchy_merge_statement(count: int) -> TextClause:
    """Batched hierarchy UPDATE for engines that support MERGE."""
    source = " UNION ALL ".join(
        "SELECT " + ", ".join(_source_column(c, i, _HIERARCHY_CASTS) for c in _HIERARCHY_COLUMNS)
        for i in range(count)
    )
    assignments = ", ".join(f"{c} = source.{c}" for c in _HIERARCHY_COLUMNS if c != "series_id")
    return text(
        f"MERGE INTO {_HIERARCHY} AS target USING ({source}) AS source "
        f"ON target.series_id = source.series_id WHEN MATCHED THEN UPDATE SET {assignments}"
    )


def build_hierarchy(catalog: dict[str, dict[str, Any]]) -> list[HierarchyNode]:
    """Describe every published index identity; names come from the source."""
    nodes = []
    for series_id, entry in sorted(catalog.items()):
        native = series_id.removeprefix(PREFIX)
        nodes.append(
            HierarchyNode(
                series_id, native, parent_id(native), hierarchy_level(native), str(entry["name"])
            )
        )
    return nodes


def upsert_original_weights(
    conn: Connection, weights: list[BaseWeight], collected_at: datetime
) -> WeightWriteResult:
    """Apply the fleet vintage rules to the published Table 8 cells."""
    today = collected_at.date()
    published = [w for w in weights if w.parent_id is not None]
    ids = sorted({w.series_id for w in published})
    existing: dict[tuple[str, date], dict[str, Any]] = {}
    for start in range(0, len(ids), 50):
        rows = (
            conn.execute(_LATEST_WEIGHTS_SQL, {"series_ids": ids[start : start + 50]})
            .mappings()
            .all()
        )
        existing.update(
            {(str(r["series_id"]), _as_date(r["reference_date"])): dict(r) for r in rows}
        )
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    new_vintages = 0
    for weight in published:
        row = {
            "series_id": weight.series_id,
            "reference_date": weight.base_period,
            "vintage_date": today,
            "weight": weight.percent,
            "weight_base_year": weight.base_period.year,
            "collected_at": collected_at,
        }
        current = existing.get((weight.series_id, weight.base_period))
        if current is None:
            inserts.append(row)
        elif round(float(current["weight"]), ROUND_DECIMALS) == round(
            weight.percent, ROUND_DECIMALS
        ):
            continue
        elif _as_date(current["vintage_date"]) == today:
            updates.append(row)
        else:
            inserts.append(row)
            new_vintages += 1
    for start in range(0, len(inserts), BATCH_SIZE):
        batch = inserts[start : start + BATCH_SIZE]
        conn.execute(
            _insert_statement(_WEIGHTS, _WEIGHT_COLUMNS, len(batch)),
            _parameters(batch, _WEIGHT_COLUMNS),
        )
    if updates:
        if conn.dialect.name in _WEIGHT_MERGE_DIALECTS:
            for start in range(0, len(updates), BATCH_SIZE):
                batch = updates[start : start + BATCH_SIZE]
                conn.execute(
                    weight_merge_statement(len(batch)), _parameters(batch, _WEIGHT_COLUMNS)
                )
        else:
            conn.execute(_UPDATE_WEIGHT_SQL, updates)
    result = WeightWriteResult(len(inserts) - new_vintages, new_vintages, len(updates))
    logger.info("Original weights upsert: %s", result)
    return result


def upsert_hierarchy(
    conn: Connection, nodes: list[HierarchyNode], collected_at: datetime
) -> tuple[int, int]:
    """Insert new identities, correct changed ones, leave unchanged rows alone."""
    existing = {
        str(r["series_id"]): dict(r) for r in conn.execute(_SELECT_HIERARCHY_SQL).mappings().all()
    }
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for node in nodes:
        row = {
            "series_id": node.series_id,
            "native_code": node.native_code,
            "parent_id": node.parent_id,
            "level": node.level,
            "name": node.name,
            "collected_at": collected_at,
        }
        current = existing.get(node.series_id)
        if current is None:
            inserts.append(row)
        elif any(row[c] != current[c] for c in _HIERARCHY_COMPARABLE):
            updates.append(row)
    for start in range(0, len(inserts), BATCH_SIZE):
        batch = inserts[start : start + BATCH_SIZE]
        conn.execute(
            _insert_statement(_HIERARCHY, _HIERARCHY_COLUMNS, len(batch)),
            _parameters(batch, _HIERARCHY_COLUMNS),
        )
    if updates:
        if conn.dialect.name in _HIERARCHY_MERGE_DIALECTS:
            for start in range(0, len(updates), BATCH_SIZE):
                batch = updates[start : start + BATCH_SIZE]
                conn.execute(
                    hierarchy_merge_statement(len(batch)), _parameters(batch, _HIERARCHY_COLUMNS)
                )
        else:
            conn.execute(_UPDATE_HIERARCHY_SQL, updates)
    logger.info("Hierarchy upsert: inserted=%d updated=%d", len(inserts), len(updates))
    return len(inserts), len(updates)
