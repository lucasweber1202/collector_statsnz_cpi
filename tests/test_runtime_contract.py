"""Check production routing without creating credentials or contacting a warehouse."""
from unittest.mock import Mock, patch

import httpx
import pytest

from scripts import db, extract


def test_production_routes_to_canonical_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "PROD", True)
    sentinel = Mock()
    with patch("scripts.databricks_engine.get_orm_engine", return_value=sentinel) as build:
        assert db.build_engine() is sentinel
        build.assert_called_once_with(db.CATALOG_NAME, db.SCHEMA_NAME)


def test_http_retry_recovers_from_service_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract.time, "sleep", lambda delay: None)
    calls = []
    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, content=b"ok")
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        response = extract._http_get(client, "https://example.invalid/source")
    assert response.content == b"ok"
    assert len(calls) == 2


def test_pg_vintages_support_asof_and_preserve_utc() -> None:
    import os
    from datetime import UTC, date, datetime, timedelta

    from sqlalchemy import create_engine, text

    from scripts import init_db, time_series
    from scripts.config import SCHEMA_NAME
    url = os.getenv("COLLECTOR_TEST_PG_URL")
    if not url:
        pytest.skip("requires disposable PostgreSQL")
    engine = create_engine(url)
    init_db.init_db(engine)
    sid = "TEST_RUNTIME_ASOF"
    ref = date(2026, 6, 30)
    first = datetime(2026, 9, 20, 23, 30, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {SCHEMA_NAME}.time_series WHERE series_id=:s"), {"s": sid})
        points = [time_series.Observation(sid, ref, 100.0, "a")]
        assert time_series.upsert_time_series(conn, points, first).new_observations == 1
        revised = [time_series.Observation(sid, ref, 101.0, "b")]
        assert time_series.upsert_time_series(conn, revised, first + timedelta(days=1)).new_vintages == 1
        sql = text(f"SELECT value, collected_at FROM {SCHEMA_NAME}.time_series WHERE series_id=:s AND vintage_date<=:d ORDER BY vintage_date DESC, collected_at DESC LIMIT 1")
        old = conn.execute(sql, {"s": sid, "d": first.date()}).one()
        assert old.value == 100.0
        assert old.collected_at == first.replace(tzinfo=None)
        assert conn.execute(sql, {"s": sid, "d": (first + timedelta(days=1)).date()}).one().value == 101.0
        conn.execute(text(f"DELETE FROM {SCHEMA_NAME}.time_series WHERE series_id=:s"), {"s": sid})
    engine.dispose()
