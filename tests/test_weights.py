"""Verify official base weights and hierarchy; source XLSX is opt-in."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from scripts.extract import parse_csv
from scripts.weight_sources import export_validation_workbook, parent_id, parse_base_weights


def test_hierarchy() -> None:
    assert parent_id("SE9A") is None
    assert parent_id("SE901") == "STATSNZ_CPI_CPIQ_SE9A"
    assert parent_id("SE9011") == "STATSNZ_CPI_CPIQ_SE901"
    assert parent_id("SE901101") == "STATSNZ_CPI_CPIQ_SE9011"
    with pytest.raises(ValueError):
        parent_id("unexpected")


@pytest.mark.skipif(not os.getenv("CPI_OFFICIAL_FILES"), reason="opt-in Stats NZ files")
def test_official_weights(tmp_path: Path) -> None:
    root = Path(os.environ["CPI_OFFICIAL_FILES"])
    parsed = parse_csv(
        (root / "cpi-index.csv").read_bytes(),
        "https://www.stats.govt.nz/source.csv",
        date(2026, 7, 21),
    )
    weights = parse_base_weights((root / "cpi-nz.xlsx").read_bytes(), parsed.catalog)
    assert len(weights) == 599
    assert len({w.series_id for w in weights}) == 150
    assert (
        next(
            w.headline_share
            for w in weights
            if w.series_id == "STATSNZ_CPI_CPIQ_SE901" and w.base_period == date(2024, 12, 31)
        )
        == 0.1845
    )
    book = tmp_path / "validation.xlsx"
    export_validation_workbook(str(book), weights, parsed.catalog)
    assert book.stat().st_size > 1000
