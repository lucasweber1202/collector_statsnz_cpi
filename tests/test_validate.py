"""Target validation on a synthetic release built with the official formula."""

from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import date

import pytest

from scripts.extract import SourceData
from scripts.time_series import Observation
from scripts.validate import (
    HEADLINE,
    TargetValidationError,
    validate_aggregation,
    validate_hierarchy,
    validate_weights,
)
from scripts.weight_sources import REGIMES, BaseWeight

G1, G2 = "STATSNZ_CPI_CPIQ_SE901", "STATSNZ_CPI_CPIQ_SE902"
C1 = "STATSNZ_CPI_CPIQ_SE9011"
C11 = "STATSNZ_CPI_CPIQ_SE901101"
SHARES = {G1: 60.0, G2: 40.0}


def _catalog() -> dict[str, dict[str, str]]:
    labels = {
        HEADLINE: "All Groups",
        G1: "Level 1 Groups",
        G2: "Level 1 Groups",
        C1: "Level 2 Subgroups",
        C11: "Level 3 Classes",
    }
    return {
        sid: {
            "name": sid,
            "description": f"Stats NZ CPI CPI {label} for New Zealand; official code x",
        }
        for sid, label in labels.items()
    }


def _weights() -> list[BaseWeight]:
    out = []
    for base in REGIMES.values():
        out.append(BaseWeight(HEADLINE, None, base, 1.0, None, "All groups", 100.0))
        for sid, pct in SHARES.items():
            out.append(BaseWeight(sid, HEADLINE, base, pct / 100, pct / 100, sid, pct))
        out.append(BaseWeight(C1, G1, base, 0.6, 1.0, "sub", 60.0))
    return out


def _quarters() -> list[date]:
    out = []
    for year in range(2014, 2027):
        for month in (3, 6, 9, 12):
            q = date(year, month, calendar.monthrange(year, month)[1])
            if date(2014, 6, 30) <= q <= date(2026, 6, 30):
                out.append(q)
    return out


def _observations(shift: float = 0.0) -> list[Observation]:
    level = {G1: 1000.0, G2: 1000.0}
    headline = 1000.0
    base_levels = dict(level)
    base_headline = headline
    bases = set(REGIMES.values())
    out = []
    for i, q in enumerate(_quarters()):
        if i:
            level = {
                G1: level[G1] * (1.01 + 0.002 * (i % 3)),
                G2: level[G2] * (1.004 - 0.001 * (i % 2)),
            }
            headline = base_headline * sum(
                SHARES[s] / 100 * level[s] / base_levels[s] for s in SHARES
            )
        out += [
            Observation(G1, q, level[G1], "s"),
            Observation(G2, q, level[G2], "s"),
            Observation(HEADLINE, q, headline * (1 + shift if q == date(2021, 3, 31) else 1), "s"),
        ]
        if q in bases:
            base_levels, base_headline = dict(level), headline
    return out


def test_formula_reproduces_the_headline_exactly() -> None:
    checked, worst = validate_aggregation(_observations(), _weights(), exceptions={})
    assert checked > 40 and worst < 1e-12


def test_a_real_aggregation_break_fails() -> None:
    with pytest.raises(TargetValidationError, match="aggregation off"):
        validate_aggregation(_observations(shift=0.002), _weights(), exceptions={})


def test_weights_and_single_child_inheritance_pass() -> None:
    assert validate_weights(_weights(), _catalog()) == 1  # C11 inherits C1's weight


def test_duplicate_weight_fails() -> None:
    weights = _weights()
    with pytest.raises(TargetValidationError, match="Duplicate"):
        validate_weights([*weights, weights[1]], _catalog())


def test_orphan_weight_fails() -> None:
    orphan = replace(_weights()[1], series_id="STATSNZ_CPI_CPIQ_SE903")
    with pytest.raises(TargetValidationError, match="without index"):
        validate_weights([*_weights(), orphan], _catalog())


def test_top_level_sum_must_be_one_hundred() -> None:
    weights = [replace(w, percent=w.percent + 1) if w.series_id == G2 else w for w in _weights()]
    with pytest.raises(TargetValidationError, match="Top-level"):
        validate_weights(weights, _catalog())


def test_children_above_parent_fail() -> None:
    weights = [replace(w, percent=70.0) if w.series_id == C1 else w for w in _weights()]
    with pytest.raises(TargetValidationError, match="exceed"):
        validate_weights(weights, _catalog())


def test_unexplained_shortfall_fails() -> None:
    weights = [replace(w, percent=50.0) if w.series_id == C1 else w for w in _weights()]
    with pytest.raises(TargetValidationError, match="short"):
        validate_weights(weights, _catalog())


def test_weightless_series_that_is_not_an_only_child_fails() -> None:
    catalog = _catalog()
    catalog["STATSNZ_CPI_CPIQ_SE901102"] = catalog[C11]
    with pytest.raises(TargetValidationError, match="only child"):
        validate_weights(_weights(), catalog)


def test_hierarchy_label_and_parent_checks() -> None:
    validate_hierarchy(_catalog())
    bad = _catalog()
    bad[G1] = {"name": G1, "description": "Stats NZ CPI CPI Level 3 Classes for New Zealand; x"}
    with pytest.raises(TargetValidationError, match="disagrees"):
        validate_hierarchy(bad)
    orphan = _catalog()
    del orphan[C1]
    with pytest.raises(TargetValidationError, match="no published parent"):
        validate_hierarchy(orphan)


def test_source_data_carries_release_fields() -> None:
    data = SourceData([], {})
    assert data.release is None and data.workbook == b"" and data.source_catalog is None


def test_pinned_exception_must_stay_pinned() -> None:
    with pytest.raises(TargetValidationError, match="moved"):
        validate_aggregation(_observations(), _weights(), exceptions={date(2021, 3, 31): 0.0013})
