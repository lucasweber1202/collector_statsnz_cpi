"""Exercise every write path against a real PostgreSQL server.

SQLite is not evidence for production SQL: it accepts untyped NULLs in a MERGE
source and has no MERGE at all. These tests run only when
``COLLECTOR_TEST_PG_URL`` points at a disposable PostgreSQL database (they drop
and recreate this collector's schema there), e.g.::

    COLLECTOR_TEST_PG_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/kinea_test pytest
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from scripts import init_db, metadata, original_weights, run_logs, time_series
from scripts.config import SCHEMA_NAME
from scripts.extract import parse_csv
from scripts.releases import (
    FIRST_RELEASE,
    NEW_RELEASE,
    REVISED_SOURCE,
    SAME_RELEASE,
    classify_release,
    stored_release,
)
from scripts.time_series import Observation
from scripts.weight_sources import BaseWeight

PG_URL = os.getenv("COLLECTOR_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="set COLLECTOR_TEST_PG_URL to a disposable PostgreSQL database"
)

HEADLINE = "STATSNZ_CPI_CPIQ_SE9A"
FOOD = "STATSNZ_CPI_CPIQ_SE901"
DAY1 = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
CSV = (
    b"Series_reference,Period,Data_value,STATUS,UNITS,Subject,Group,Series_title_1,Series_title_2\n"
    b"CPIQ.SE9A,2026.03,1339,FINAL,Index,CPI,CPI All Groups for New Zealand,All groups,NA\n"
    b"CPIQ.SE9A,2026.06,1359,FINAL,Index,CPI,CPI All Groups for New Zealand,All groups,NA\n"
    b"CPIQ.SE901,2026.03,1373,FINAL,Index,CPI,CPI Level 1 Groups for New Zealand,Food,NA\n"
    b"CPIQ.SE901,2026.06,1379,FINAL,Index,CPI,CPI Level 1 Groups for New Zealand,Food,NA\n"
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(PG_URL)
    assert eng.dialect.name == "postgresql"
    with eng.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE"))
    init_db.init_db(eng)
    yield eng
    with eng.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE"))
    eng.dispose()


def _sample() -> tuple[list[Observation], dict[str, dict[str, Any]]]:
    parsed = parse_csv(CSV, "https://www.stats.govt.nz/test.csv", date(2026, 7, 21), min_rows=1)
    return parsed.observations, parsed.catalog


def _write(
    eng: Engine, observations: list[Observation], catalog: dict[str, dict[str, Any]], at: datetime
) -> Any:
    with eng.begin() as conn:
        result = time_series.upsert_time_series(conn, observations, at)
        counts = metadata.upsert_metadata(conn, catalog, at)
    return result, counts


def _rows(eng: Engine, table: str) -> list[dict[str, Any]]:
    with eng.connect() as conn:
        return [
            dict(r) for r in conn.execute(text(f"SELECT * FROM {SCHEMA_NAME}.{table}")).mappings()
        ]


def test_init_db_creates_every_table_idempotently(engine: Engine) -> None:
    init_db.init_db(engine)
    with engine.connect() as conn:
        tables: set[str] = set(
            conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
                {"s": SCHEMA_NAME},
            ).scalars()
        )
    assert tables == {"metadata", "time_series", "logs", "weights", "original_weights", "cpi_hierarchy"}


def test_first_run_then_unchanged_rerun_is_a_no_op(engine: Engine) -> None:
    observations, catalog = _sample()
    result, counts = _write(engine, observations, catalog, DAY1)
    assert (result.new_observations, counts) == (4, (2, 0))
    before_ts, before_md = _rows(engine, "time_series"), _rows(engine, "metadata")
    result, counts = _write(engine, observations, catalog, DAY1 + timedelta(days=3))
    assert (result.new_observations, result.new_vintages, result.same_day_updates, counts) == (
        0,
        0,
        0,
        (0, 0),
    )
    assert _rows(engine, "time_series") == before_ts
    assert _rows(engine, "metadata") == before_md
    headline = next(r for r in before_md if r["series_id"] == HEADLINE)
    assert (headline["country"], headline["frequency"], headline["unit"]) == (
        "NZD",
        "quarterly",
        "index",
    )
    assert (
        headline["first_observation"],
        headline["last_observation"],
        headline["observation_count"],
    ) == (
        date(2026, 3, 31),
        date(2026, 6, 30),
        2,
    )
    assert headline["last_publish_date"] == date(2026, 7, 21)
    assert {r["vintage_date"] for r in before_ts} == {DAY1.date()}


def test_later_revision_adds_a_vintage_and_keeps_the_old_one(engine: Engine) -> None:
    observations, catalog = _sample()
    _write(engine, observations, catalog, DAY1)
    revised = [
        Observation(o.series_id, o.reference_date, o.value + 1, o.snapshot_id)
        if o.series_id == FOOD and o.reference_date == date(2026, 6, 30)
        else o
        for o in observations
    ]
    later = DAY1 + timedelta(days=90)
    result, _ = _write(engine, revised, catalog, later)
    assert (result.new_observations, result.new_vintages, result.same_day_updates) == (0, 1, 0)
    food = sorted(
        (r["vintage_date"], r["value"])
        for r in _rows(engine, "time_series")
        if r["series_id"] == FOOD and r["reference_date"] == date(2026, 6, 30)
    )
    assert food == [(DAY1.date(), 1379.0), (later.date(), 1380.0)]
    md = next(r for r in _rows(engine, "metadata") if r["series_id"] == FOOD)
    assert md["observation_count"] == 2  # distinct reference dates, not vintages


def test_same_day_revision_updates_that_days_vintage(engine: Engine) -> None:
    observations, catalog = _sample()
    _write(engine, observations, catalog, DAY1)
    revised = [
        Observation(o.series_id, o.reference_date, 1360.0, o.snapshot_id)
        if o.series_id == HEADLINE and o.reference_date == date(2026, 6, 30)
        else o
        for o in observations
    ]
    result, _ = _write(engine, revised, catalog, DAY1 + timedelta(hours=2))
    assert (result.new_observations, result.new_vintages, result.same_day_updates) == (0, 0, 1)
    rows = [
        r
        for r in _rows(engine, "time_series")
        if r["series_id"] == HEADLINE and r["reference_date"] == date(2026, 6, 30)
    ]
    assert len(rows) == 1 and rows[0]["value"] == 1360.0
    assert rows[0]["collected_at"] == (DAY1 + timedelta(hours=2)).replace(tzinfo=None)


def test_metadata_merge_accepts_null_in_every_nullable_column(engine: Engine) -> None:
    observations, catalog = _sample()
    _write(engine, observations, catalog, DAY1)
    row: dict[str, Any] = {column: None for column in metadata._COLUMNS}
    row.update(
        series_id=HEADLINE,
        name="All groups",
        country="NZD",
        observation_count=2,
        source_url="https://www.stats.govt.nz/x",
        eco_group=catalog[HEADLINE]["eco_group"],
        last_publish_date=DAY1.date(),
        collected_at=DAY1,
    )
    with engine.begin() as conn:
        conn.execute(metadata._merge_statement(1), metadata._batch_parameters([row]))
    stored = next(r for r in _rows(engine, "metadata") if r["series_id"] == HEADLINE)
    for column in (
        "description",
        "frequency",
        "unit",
        "first_observation",
        "last_observation",
    ):
        assert stored[column] is None, column
    # The normal upsert then restores the described row through the same MERGE.
    _, counts = _write(engine, observations, catalog, DAY1 + timedelta(days=1))
    assert counts == (0, 1)


def test_time_series_merge_statement_runs_on_postgres(engine: Engine) -> None:
    observations, catalog = _sample()
    _write(engine, observations, catalog, DAY1)
    row = {
        "series_id": HEADLINE,
        "reference_date": date(2026, 6, 30),
        "vintage_date": DAY1.date(),
        "value": 1361.5,
        "collected_at": DAY1 + timedelta(hours=1),
    }
    with engine.begin() as conn:
        conn.execute(time_series._merge_statement(1), time_series._batch_parameters([row]))
    rows = [
        r
        for r in _rows(engine, "time_series")
        if r["series_id"] == HEADLINE and r["reference_date"] == date(2026, 6, 30)
    ]
    assert [r["value"] for r in rows] == [1361.5]


def test_run_log_insert_with_null_traceback_and_truncation(engine: Engine) -> None:
    run_logs.insert_run_log(engine, DAY1, DAY1, "success", "ok", None)
    run_logs.insert_run_log(engine, DAY1, DAY1, "error", "x" * 70000, "trace")
    rows = sorted(_rows(engine, "logs"), key=lambda r: r["id"])
    assert rows[0]["traceback"] is None and rows[0]["status"] == "success"
    assert len(rows[1]["log_text"]) == 65535 and rows[1]["log_text"].endswith(
        "[..., truncated ...]"
    )


def _weights(value: float) -> list[BaseWeight]:
    base = date(2024, 12, 31)
    return [
        BaseWeight(HEADLINE, None, base, 1.0, None, "All groups", 100.0),
        BaseWeight(FOOD, HEADLINE, base, value / 100, value / 100, "Food group", value),
    ]


def test_original_weights_vintages_and_same_day_rule(engine: Engine) -> None:
    with engine.begin() as conn:
        first = original_weights.upsert_original_weights(conn, _weights(18.45), DAY1)
        again = original_weights.upsert_original_weights(
            conn, _weights(18.45), DAY1 + timedelta(days=1)
        )
        same_day = original_weights.upsert_original_weights(
            conn, _weights(18.46), DAY1 + timedelta(hours=1)
        )
        later = original_weights.upsert_original_weights(
            conn, _weights(18.47), DAY1 + timedelta(days=5)
        )
    assert (first.new_weights, again.new_weights, again.new_vintages) == (1, 0, 0)
    assert (same_day.same_day_updates, later.new_vintages) == (1, 1)
    rows = sorted(
        (r["vintage_date"], r["weight"], r["weight_base_year"])
        for r in _rows(engine, "original_weights")
    )
    assert rows == [(DAY1.date(), 18.46, 2024), ((DAY1 + timedelta(days=5)).date(), 18.47, 2024)]
    assert all(r["series_id"] != HEADLINE for r in _rows(engine, "original_weights"))
    with engine.begin() as conn:
        conn.execute(
            original_weights.weight_merge_statement(1),
            original_weights._parameters(
                [
                    {
                        "series_id": FOOD,
                        "reference_date": date(2024, 12, 31),
                        "vintage_date": DAY1.date(),
                        "weight": 1.5,
                        "weight_base_year": 2024,
                        "collected_at": DAY1,
                    }
                ],
                original_weights._WEIGHT_COLUMNS,
            ),
        )
    assert sorted(r["weight"] for r in _rows(engine, "original_weights")) == [1.5, 18.47]


def test_hierarchy_upsert_and_null_parent_merge(engine: Engine) -> None:
    _, catalog = _sample()
    nodes = original_weights.build_hierarchy(catalog)
    with engine.begin() as conn:
        assert original_weights.upsert_hierarchy(conn, nodes, DAY1) == (2, 0)
        assert original_weights.upsert_hierarchy(conn, nodes, DAY1 + timedelta(days=1)) == (0, 0)
    stored = {r["series_id"]: r for r in _rows(engine, "cpi_hierarchy")}
    assert stored[HEADLINE]["parent_id"] is None and stored[HEADLINE]["level"] == 0
    assert stored[FOOD]["parent_id"] == HEADLINE and stored[FOOD]["level"] == 1
    assert stored[FOOD]["collected_at"] == DAY1.replace(tzinfo=None)
    row = {
        "series_id": FOOD,
        "native_code": "SE901",
        "parent_id": None,
        "level": 1,
        "name": "Food",
        "collected_at": DAY1,
    }
    with engine.begin() as conn:
        conn.execute(
            original_weights.hierarchy_merge_statement(1),
            original_weights._parameters([row], original_weights._HIERARCHY_COLUMNS),
        )
    assert {r["series_id"]: r for r in _rows(engine, "cpi_hierarchy")}[FOOD]["parent_id"] is None
    with engine.begin() as conn:
        assert original_weights.upsert_hierarchy(conn, nodes, DAY1 + timedelta(days=2)) == (0, 1)


def test_release_status_from_stored_state(engine: Engine) -> None:
    observations, catalog = _sample()
    ids = sorted(catalog)
    with engine.connect() as conn:
        assert stored_release(conn, ids) is None
    assert classify_release(None, date(2026, 7, 21), date(2026, 6, 30), 4) == FIRST_RELEASE
    _write(engine, observations, catalog, DAY1)
    with engine.connect() as conn:
        previous = stored_release(conn, ids)
    assert previous is not None and previous.published == date(2026, 7, 21)
    assert classify_release(previous, date(2026, 7, 21), date(2026, 6, 30), 0) == SAME_RELEASE
    assert classify_release(previous, date(2026, 7, 21), date(2026, 6, 30), 1) == REVISED_SOURCE
    assert classify_release(previous, date(2026, 10, 20), date(2026, 9, 30), 2) == NEW_RELEASE


@pytest.mark.parametrize("column", ["eco_group", "last_publish_date"])
def test_required_metadata_columns_reject_null(engine: Engine, column: str) -> None:
    """Masuko section 3 requires both fields even for undated sources."""
    observations, catalog = _sample()
    _write(engine, observations, catalog, DAY1)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(f"UPDATE {SCHEMA_NAME}.metadata SET {column} = NULL"))


def test_metadata_fallback_uses_stored_collection_date(engine: Engine) -> None:
    """An undated source gets MAX(collected_at), not the reference date."""
    observations, catalog = _sample()
    undated = {sid: {**fields, "last_publish_date": None} for sid, fields in catalog.items()}
    _write(engine, observations, undated, DAY1)
    before = _rows(engine, "metadata")
    assert {row["last_publish_date"] for row in before} == {DAY1.date()}
    _, counts = _write(engine, observations, undated, DAY1 + timedelta(days=3))
    assert counts == (0, 0)
    assert _rows(engine, "metadata") == before


def test_metadata_batches_log_progress(engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    """An operator sees each completed metadata batch in the captured log."""
    observations, catalog = _sample()
    with caplog.at_level("INFO", logger="scripts.metadata"):
        _write(engine, observations, catalog, DAY1)
        changed = {
            sid: {**fields, "description": "revised description"} for sid, fields in catalog.items()
        }
        _write(engine, observations, changed, DAY1 + timedelta(days=1))
    assert "Inserted batch 1/1" in caplog.text
    assert "Updated batch 1/1" in caplog.text
