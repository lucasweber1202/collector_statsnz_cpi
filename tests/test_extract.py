"""Validate native hierarchy and historical zero placeholder handling."""
import os
from datetime import date

import pytest

from scripts.extract import build_series_id, collect, parse_csv, parse_series_id


def test_native_id_roundtrip() -> None:
    assert parse_series_id(build_series_id("CPIQ.SE9A")) == "CPIQ.SE9A"
    with pytest.raises(ValueError):
        parse_series_id("STATSNZ_CPI_WRONG")


def test_zero_placeholder_is_not_observation() -> None:
    source = b'Series_reference,Period,Data_value,STATUS,UNITS,Subject,Group,Series_title_1,Series_title_2\nCPIQ.SE9A,1914.06,12,FINAL,Index,CPI,CPI All Groups for New Zealand,All groups,NA\nCPIQ.SE9A,1914.09,0,FINAL,Index,CPI,CPI All Groups for New Zealand,All groups,NA\n'
    result = parse_csv(source, "https://www.stats.govt.nz/test.csv", date(2026, 7, 21))
    assert len(result.observations) == 1
    assert result.observations[0].value == 12
    assert result.observations[0].reference_date == date(1914, 6, 30)


@pytest.mark.skipif(os.getenv("CPI_LIVE_SMOKE") != "1", reason="opt-in official network smoke")
def test_official_all_groups() -> None:
    result = collect()
    selected = {o.reference_date: o.value for o in result.observations if o.series_id == build_series_id("CPIQ.SE9A")}
    assert selected[date(2026, 6, 30)] == 1359
    assert selected[date(2026, 3, 31)] == 1339
    assert len(result.catalog) > 100
