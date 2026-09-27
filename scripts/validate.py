"""Validate the official CPI release before any of it is persisted.

Every check here reads only published Stats NZ figures. A failure is a
``TargetValidationError`` and the run stops before the write transaction, so a
release that no longer satisfies the audited contract never reaches the tables.

The aggregation check is the strongest one. Stats NZ publishes Table 8 base
expenditure weights (percent of all-groups expenditure) at four price-reference
quarters. Used as Lowe shares on the group index relatives to that quarter,

    I_all(t) = I_all(b) * sum_g w_g(b) * I_g(t) / I_g(b)      for b < t <= next b

they reproduce the published all-groups index. The residual comes from the
published rounding (index levels to whole points, weights to two decimals). On
the June 2026 release it stayed within +/-0.063% in 47 of the 48 checked
quarters from September 2014 to June 2026; the one exception is pinned below.
This validates the published weights and hierarchy against the published index. It is not a claim that the chained CPI can be rebuilt exactly:
Stats NZ aggregates unrounded elementary indices, which are not published.
"""

from __future__ import annotations

import calendar
import io
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

import openpyxl

from scripts.extract import SourceData, SourceLayoutError
from scripts.time_series import Observation
from scripts.weight_sources import REGIMES, BaseWeight, hierarchy_level, parent_id

logger = logging.getLogger(__name__)
HEADLINE = "STATSNZ_CPI_CPIQ_SE9A"
PREFIX = "STATSNZ_CPI_CPIQ_"
# Maximum |reconstructed / published - 1| for the group-level aggregation.
AGGREGATION_TOLERANCE = 0.001
# Quarters where the published material does not explain the gap, pinned to the
# measured error so that a change in either direction still fails. September
# 2024 sits in the June 2020 basket (the December 2024 weights apply from March
# 2025, Stats NZ "Consumers price index review: 2024") and rebuilds 0.134% high.
KNOWN_AGGREGATION_EXCEPTIONS = {date(2024, 9, 30): 0.001339}
# Published top-level weights sum to 100 within rounding (99.99..100.01).
TOP_LEVEL_TOLERANCE = 0.05
# Children published below their parent by more than rounding. Each entry is a
# component that existed in that basket but has no index or Table 8 row in the
# current release. Anything new fails the run.
KNOWN_UNPUBLISHED_COMPONENTS = {(PREFIX + "SE909", date(2014, 6, 30)): 1.13}
GROUP_LABELS = {
    0: "CPI All Groups for New Zealand",
    1: "CPI Level 1 Groups for New Zealand",
    2: "CPI Level 2 Subgroups for New Zealand",
    3: "CPI Level 3 Classes for New Zealand",
}
MONTHS = {name: number for number, name in enumerate(calendar.month_abbr) if name}


class TargetValidationError(ValueError):
    """The release breaks a validated target invariant."""


@dataclass(frozen=True)
class ValidationReport:
    index_series: int
    weight_records: int
    weight_series: int
    inherited_weight_series: int
    regimes: int
    quarters_checked: int
    max_aggregation_error: float
    cross_file_points: int


def _native(series_id: str) -> str:
    return series_id.removeprefix(PREFIX)


def _quarters(start: date, end: date) -> list[date]:
    """Quarter ends strictly after ``start`` up to and including ``end``."""
    out: list[date] = []
    year, month = start.year, start.month
    while True:
        month += 3
        if month > 12:
            month, year = month - 12, year + 1
        quarter = date(year, month, calendar.monthrange(year, month)[1])
        if quarter > end:
            return out
        out.append(quarter)


def validate_hierarchy(catalog: dict[str, dict[str, Any]]) -> None:
    """Every identity has one known level, a label that agrees, and a parent."""
    roots = [sid for sid in catalog if parent_id(_native(sid)) is None]
    if roots != [HEADLINE]:
        raise TargetValidationError(f"Expected one all-groups root, found {roots}")
    for sid, entry in catalog.items():
        level = hierarchy_level(_native(sid))
        if not entry["description"].startswith(f"Stats NZ CPI {GROUP_LABELS[level]};"):
            raise TargetValidationError(f"{sid} code level {level} disagrees with its group label")
        parent = parent_id(_native(sid))
        if parent is not None and parent not in catalog:
            raise TargetValidationError(f"{sid} has no published parent {parent}")


def validate_weights(weights: list[BaseWeight], catalog: dict[str, dict[str, Any]]) -> int:
    """Check duplicates, orphans, base periods, sums, and weightless series.

    Returns how many index series carry no Table 8 row and inherit their
    parent's weight because they are its only child.
    """
    keys = [(w.series_id, w.base_period) for w in weights]
    if len(keys) != len(set(keys)):
        raise TargetValidationError("Duplicate (series_id, base_period) weight")
    if {w.base_period for w in weights} != set(REGIMES.values()):
        raise TargetValidationError("Unexpected Table 8 base periods")
    orphans = sorted({w.series_id for w in weights} - set(catalog))
    if orphans:
        raise TargetValidationError(f"Weights without index series: {orphans}")
    by_key = {(w.series_id, w.base_period): w for w in weights}
    children: dict[tuple[str, date], list[BaseWeight]] = defaultdict(list)
    for w in weights:
        if w.parent_id is not None:
            children[(w.parent_id, w.base_period)].append(w)
    for base in REGIMES.values():
        top = sum(w.percent for w in children[(HEADLINE, base)])
        if abs(top - 100.0) > TOP_LEVEL_TOLERANCE:
            raise TargetValidationError(f"Top-level weights sum to {top:.2f} at {base}")
    for (parent, base), kids in children.items():
        if parent == HEADLINE:
            continue
        total = sum(w.percent for w in kids)
        stated = by_key[(parent, base)].percent
        rounding = 0.005 * (len(kids) + 1) + 1e-9
        gap = stated - total
        if gap < -rounding:
            raise TargetValidationError(f"Children of {parent} exceed it at {base}: {total:.2f} > {stated:.2f}")
        if gap > rounding:
            known = KNOWN_UNPUBLISHED_COMPONENTS.get((parent, base))
            if known is None or abs(known - gap) > rounding:
                raise TargetValidationError(f"Children of {parent} fall {gap:.2f} short at {base}")
    weighted = {w.series_id for w in weights}
    index_children: dict[str, list[str]] = defaultdict(list)
    for sid in catalog:
        owner = parent_id(_native(sid))
        if owner is not None:
            index_children[owner].append(sid)
    inherited = 0
    for sid in catalog:
        if sid in weighted:
            continue
        owner = parent_id(_native(sid))
        if owner is None or owner not in weighted or len(index_children[owner]) != 1:
            raise TargetValidationError(f"{sid} has no Table 8 weight and is not its parent's only child")
        inherited += 1
    return inherited


def validate_aggregation(
    observations: list[Observation],
    weights: list[BaseWeight],
    exceptions: dict[date, float] = KNOWN_AGGREGATION_EXCEPTIONS,
) -> tuple[int, float]:
    """Rebuild all-groups from group indices and published weights, per regime."""
    index = {(o.series_id, o.reference_date): o.value for o in observations}
    bases = sorted(REGIMES.values())
    last = max(o.reference_date for o in observations if o.series_id == HEADLINE)
    checked = 0
    worst = 0.0
    for position, base in enumerate(bases):
        end = bases[position + 1] if position + 1 < len(bases) else last
        groups = [w for w in weights if w.base_period == base and w.parent_id == HEADLINE]
        total = sum(w.headline_share for w in groups)
        for quarter in _quarters(base, end):
            if (HEADLINE, quarter) not in index:
                continue
            try:
                relative = sum(
                    w.headline_share / total * index[(w.series_id, quarter)] / index[(w.series_id, base)]
                    for w in groups
                )
            except KeyError as exc:
                raise TargetValidationError(f"Group index missing for {quarter}: {exc}") from exc
            error = index[(HEADLINE, base)] * relative / index[(HEADLINE, quarter)] - 1.0
            checked += 1
            pinned = exceptions.get(quarter)
            if pinned is not None:
                if abs(error - pinned) > 0.00005:
                    raise TargetValidationError(f"Pinned aggregation gap at {quarter} moved: {error:.5%}")
                continue
            worst = max(worst, abs(error))
            if abs(error) > AGGREGATION_TOLERANCE:
                raise TargetValidationError(
                    f"All-groups aggregation off by {error:.4%} at {quarter} (base {base})"
                )
    if checked == 0:
        raise TargetValidationError("No quarter available for the aggregation check")
    return checked, worst


def summary_table_points(workbook: bytes) -> dict[tuple[str, date], float]:
    """Read Table 2.01 of the release workbook: groups, subgroups, all groups.

    The release publishes the same index levels twice, in the CSV and in this
    workbook. Reading the workbook independently lets the two official files be
    checked against each other.
    """
    book = openpyxl.load_workbook(io.BytesIO(workbook), read_only=True, data_only=True)
    if "2.01" not in book:
        raise SourceLayoutError("CPI Table 2.01 missing from the release workbook")
    rows = list(book["2.01"].values)
    if rows[5][1] != "Series ref: CPIQ" or rows[5][2] != "Quarter":
        raise SourceLayoutError("CPI Table 2.01 layout changed")
    columns: dict[int, date] = {}
    for column, label in enumerate(rows[6]):
        if isinstance(label, str) and re.fullmatch(r"[A-Z][a-z]{2}-\d{2}", label):
            month = MONTHS[label[:3]]
            year = 2000 + int(label[-2:])
            columns[column] = date(year, month, calendar.monthrange(year, month)[1])
    if len(columns) < 4:
        raise SourceLayoutError(f"CPI Table 2.01 quarter header changed: {rows[6]}")
    points: dict[tuple[str, date], float] = {}
    for row in rows[7:]:
        code = row[1]
        if not isinstance(code, str) or not code.startswith("SE9"):
            continue
        for column, quarter in columns.items():
            cell = row[column]
            if isinstance(cell, int | float):
                points[(PREFIX + code, quarter)] = float(cell)
    if (HEADLINE, max(columns.values())) not in points:
        raise SourceLayoutError("CPI Table 2.01 has no all-groups row")
    return points


def validate_cross_file(observations: list[Observation], workbook: bytes) -> int:
    """Every Table 2.01 level must equal the CSV level for the same code and quarter."""
    csv_points = {(o.series_id, o.reference_date): o.value for o in observations}
    table = summary_table_points(workbook)
    for key, value in table.items():
        if csv_points.get(key) != value:
            raise TargetValidationError(f"CSV and workbook disagree at {key}: {csv_points.get(key)} vs {value}")
    return len(table)


def validate_release(full: SourceData, weights: list[BaseWeight]) -> ValidationReport:
    """Run every check on the complete parsed release (before series filtering)."""
    validate_hierarchy(full.catalog)
    inherited = validate_weights(weights, full.catalog)
    cross_checked = validate_cross_file(full.observations, full.workbook)
    checked, worst = validate_aggregation(full.observations, weights)
    report = ValidationReport(
        index_series=len(full.catalog),
        weight_records=sum(w.parent_id is not None for w in weights),
        weight_series=len({w.series_id for w in weights if w.parent_id is not None}),
        inherited_weight_series=inherited,
        regimes=len(REGIMES),
        quarters_checked=checked,
        max_aggregation_error=worst,
        cross_file_points=cross_checked,
    )
    logger.info("Target validation: %s", report)
    return report
