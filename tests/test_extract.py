"""Validate native IDs, payload checks, placeholder handling and the live release."""
import os
from datetime import date

import httpx
import pytest

from scripts.extract import (
    SourceAccessError,
    SourceLayoutError,
    build_series_id,
    check_payload,
    collect,
    parse_csv,
    parse_release_page,
    parse_series_id,
)

HEADER = b"Series_reference,Period,Data_value,STATUS,UNITS,Subject,Group,Series_title_1,Series_title_2\n"


def test_native_id_roundtrip() -> None:
    assert parse_series_id(build_series_id("CPIQ.SE9A")) == "CPIQ.SE9A"
    with pytest.raises(ValueError):
        parse_series_id("STATSNZ_CPI_WRONG")


def test_zero_placeholder_is_not_observation() -> None:
    source = HEADER + b'CPIQ.SE9A,1914.06,12,FINAL,Index,CPI,CPI All Groups for New Zealand,All groups,NA\nCPIQ.SE9A,1914.09,0,FINAL,Index,CPI,CPI All Groups for New Zealand,All groups,NA\n'
    result = parse_csv(source, "https://www.stats.govt.nz/test.csv", date(2026, 7, 21), min_rows=1)
    assert len(result.observations) == 1
    assert result.observations[0].value == 12
    assert result.observations[0].reference_date == date(1914, 6, 30)
    assert result.catalog[build_series_id("CPIQ.SE9A")]["frequency"] == "quarterly"


def test_duplicate_key_and_short_file_fail() -> None:
    row = b"CPIQ.SE9A,2026.06,1359,FINAL,Index,CPI,CPI All Groups for New Zealand,All groups,NA\n"
    with pytest.raises(ValueError, match="Duplicate"):
        parse_csv(HEADER + row + row, "u", date(2026, 7, 21), min_rows=1)
    with pytest.raises(SourceLayoutError, match="rows"):
        parse_csv(HEADER + row, "u", date(2026, 7, 21))


def test_changed_header_is_a_layout_error() -> None:
    with pytest.raises(SourceLayoutError):
        parse_csv(b"Series,Period\nx,1\n", "u", date(2026, 7, 21), min_rows=1)


def _response(body: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": content_type}, request=httpx.Request("GET", "https://www.stats.govt.nz/f"))


def test_payload_check_rejects_html_and_wrong_magic() -> None:
    with pytest.raises(SourceAccessError, match="HTML"):
        check_payload(_response(b"<!DOCTYPE html><title>Pardon Our Interruption</title>", "text/html"), "csv")
    with pytest.raises(SourceAccessError, match="HTML"):
        check_payload(_response(b"<html>challenge</html>" * 10000, "application/octet-stream"), "xlsx")
    with pytest.raises(SourceAccessError, match="not an XLSX"):
        check_payload(_response(b"x" * 30000, "application/octet-stream"), "xlsx")
    with pytest.raises(SourceLayoutError, match="header"):
        check_payload(_response(b"Other,Header\n" * 10000, "text/csv"), "csv")
    with pytest.raises(SourceAccessError, match="small"):
        check_payload(_response(b'"Series_reference",x\n', "text/csv"), "csv")
    body = b'\xef\xbb\xbf"Series_reference","Period"\n' + b"x" * 200000
    assert check_payload(_response(body, "application/octet-stream"), "csv") == body


def test_release_page_needs_both_files_and_a_publication_date() -> None:
    markup = '"DocumentLink":"\\/a\\/cpi-june-2026-quarter-index-numbers.csv" "DocumentLink":"\\/a\\/consumers-price-index-june-2026-quarter.xlsx" "PublicationDate":"2026-07-21T10:45:00"'
    release = parse_release_page(markup, "https://www.stats.govt.nz/p/")
    assert release.published == date(2026, 7, 21)
    assert release.csv_url == "https://www.stats.govt.nz/a/cpi-june-2026-quarter-index-numbers.csv"
    assert release.workbook_url.endswith("consumers-price-index-june-2026-quarter.xlsx")
    with pytest.raises(SourceLayoutError):
        parse_release_page(markup.replace("PublicationDate", "Date"), "https://www.stats.govt.nz/p/")


@pytest.mark.skipif(os.getenv("CPI_LIVE_SMOKE") != "1", reason="opt-in official network smoke")
def test_official_release_validates_end_to_end() -> None:
    from scripts.extract import SourceData
    from scripts.validate import validate_release
    from scripts.weight_sources import parse_base_weights

    result = collect()
    assert result.release is not None and result.source_catalog is not None and result.source_observations is not None
    selected = {o.reference_date: o.value for o in result.observations if o.series_id == build_series_id("CPIQ.SE9A")}
    assert selected[date(2026, 6, 30)] == 1359
    assert selected[date(2026, 3, 31)] == 1339
    assert len(result.source_catalog) == 164
    weights = parse_base_weights(result.workbook, result.source_catalog)
    report = validate_release(SourceData(result.source_observations, result.source_catalog, result.release, result.workbook), weights)
    assert report.inherited_weight_series == 14 and report.max_aggregation_error < 0.001
