"""Exercise release-monitoring decisions and real target-weight vintage writes."""
from __future__ import annotations

from argparse import Namespace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

import main
from scripts import init_db, weights
from scripts.config import SCHEMA_NAME


def test_monitoring_empty_database_and_no_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = object()
    monkeypatch.setattr(main, 'get_last_observations', lambda engine: {})
    monkeypatch.setattr(main, 'collect', lambda: expected)
    args = Namespace(start_date=None, no_watch=False)
    assert main._collect_for_release(Mock(spec=Engine), args) is expected
    monkeypatch.setattr(main, 'get_last_observations', lambda engine: {main.HEADLINE_SERIES:date(2026,6,30)})
    args.no_watch=True
    assert main._collect_for_release(Mock(spec=Engine),args) is expected


def test_monitoring_timeout_and_new_period(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import config
    monkeypatch.setattr(config,'MAX_WAIT',0)
    monkeypatch.setattr(main,'get_last_observations',lambda engine:{main.HEADLINE_SERIES:date(2026,6,30)})
    old=SimpleNamespace(observations=[SimpleNamespace(series_id=main.HEADLINE_SERIES,reference_date=date(2026,6,30))])
    monkeypatch.setattr(main,'collect',lambda:old)
    args=Namespace(start_date=None,no_watch=False)
    assert main._collect_for_release(Mock(spec=Engine),args) is None
    new=SimpleNamespace(observations=[SimpleNamespace(series_id=main.HEADLINE_SERIES,reference_date=date(2026,9,30))])
    monkeypatch.setattr(main,'collect',lambda:new)
    assert main._collect_for_release(Mock(spec=Engine),args) is new


def test_weight_idempotency_revision_and_asof() -> None:
    import os
    url=os.getenv('COLLECTOR_TEST_PG_URL')
    if not url:
        pytest.skip('requires disposable PostgreSQL')
    engine=create_engine(url)
    init_db.init_db(engine)
    sid='TEST_TARGET_WEIGHT'
    t=datetime(2026,9,20,23,30,tzinfo=UTC)
    ref=date(2026,6,30)
    with engine.begin() as conn:
        conn.execute(text(f'DELETE FROM {SCHEMA_NAME}.weights WHERE series_id=:s'),{'s':sid})
        first=[weights.WeightObservation(sid,ref,0.25,'a')]
        assert weights.upsert_weights(conn,first,t).new_weights==1
        assert weights.upsert_weights(conn,first,t+timedelta(days=1)).written_keys==[]
        changed=[weights.WeightObservation(sid,ref,0.3,'b')]
        assert weights.upsert_weights(conn,changed,t+timedelta(days=1)).new_vintages==1
        changed=[weights.WeightObservation(sid,ref,0.35,'c')]
        assert weights.upsert_weights(conn,changed,t+timedelta(days=1,hours=0,minutes=15)).same_day_updates==1
        assert conn.execute(text(f'SELECT weight FROM {SCHEMA_NAME}.weights WHERE series_id=:s AND vintage_date<=:d ORDER BY vintage_date DESC,collected_at DESC LIMIT 1'),{'s':sid,'d':t.date()}).scalar()==0.25
        conn.execute(text(f'DELETE FROM {SCHEMA_NAME}.weights WHERE series_id=:s'),{'s':sid})
    engine.dispose()
