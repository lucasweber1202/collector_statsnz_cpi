"""Official Stats NZ base expenditure weights and native CPI hierarchy."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import openpyxl

from scripts.extract import SourceLayoutError, build_series_id

REGIMES = {2: date(2014, 6, 30), 4: date(2017, 9, 30), 6: date(2020, 6, 30), 8: date(2024, 12, 31)}


@dataclass(frozen=True)
class BaseWeight:
    """One published Table 8 cell: a base expenditure weight in percent.

    ``percent`` is the figure exactly as published (percent of all-groups
    expenditure at ``base_period``). The shares are derived from it only for
    validation and never replace it in storage.
    """

    series_id: str
    parent_id: str | None
    base_period: date
    headline_share: float
    parent_share: float | None
    label: str
    percent: float = 0.0


def hierarchy_level(native: str) -> int:
    """0 all groups, 1 group, 2 subgroup, 3 class, from the native code length."""
    if native == "SE9A":
        return 0
    levels = {5: 1, 6: 2, 8: 3}
    if not native.startswith("SE9") or len(native) not in levels:
        raise ValueError(f"Unknown CPI hierarchy code: {native}")
    return levels[len(native)]


def parent_id(native: str) -> str | None:
    """Recover the official hierarchy by native code length."""
    if native == "SE9A":
        return None
    if len(native) == 5:
        return build_series_id("CPIQ.SE9A")
    if len(native) == 6:
        return build_series_id("CPIQ." + native[:5])
    if len(native) == 8:
        return build_series_id("CPIQ." + native[:6])
    raise ValueError(f"Unknown CPI hierarchy code: {native}")


def parse_base_weights(blob: bytes, catalog: dict[str, dict[str, Any]]) -> list[BaseWeight]:
    workbook = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    if "8" not in workbook:
        raise ValueError("Official expenditure weight Table 8 missing")
    rows = list(workbook["8"].values)
    labels = {column: rows[6][column] for column in REGIMES}
    expected = {2: "June 2014", 4: "September 2017", 6: "June 2020", 8: "December 2024"}
    if (
        rows[5][1] != "Series ref: CPIQ"
        or rows[5][2] != "Base expenditure weight"
        or labels != expected
    ):
        raise SourceLayoutError(f"Stats NZ weight table layout changed: {labels}")
    raw: dict[tuple[str, date], tuple[float, str]] = {}
    for row in rows[8:]:
        native = row[1]
        if not isinstance(native, str) or not native.startswith("SE"):
            continue
        sid = build_series_id("CPIQ." + native)
        if sid not in catalog:
            # A weight identity with no published index is an orphan; the
            # current release has none, so one appearing means re-audit.
            raise SourceLayoutError(f"Table 8 weight has no CPI index series: {sid}")
        for column, base in REGIMES.items():
            value = row[column]
            if value in (None, ".."):
                continue
            if (
                not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
                or value > 100
            ):
                raise ValueError(f"Invalid base weight {sid}: {value}")
            key = sid, base
            if key in raw:
                raise ValueError(f"Duplicate weight {key}")
            raw[key] = float(value), str(row[0]).strip()
    headline = build_series_id("CPIQ.SE9A")
    for base in REGIMES.values():
        raw[headline, base] = 100.0, "All groups"
    result: list[BaseWeight] = []
    for (sid, base), (value, label) in raw.items():
        native = sid.removeprefix("STATSNZ_CPI_CPIQ_")
        parent = parent_id(native)
        parent_value = raw.get((parent, base)) if parent else None
        if parent and (parent_value is None or parent_value[0] <= 0):
            # A discontinued subcomponent may be absent in a newer regime.
            if value == 0:
                continue
            raise ValueError(f"Missing parent base weight: {sid} {base}")
        result.append(
            BaseWeight(
                sid,
                parent,
                base,
                value / 100.0,
                value / parent_value[0] if parent_value else None,
                label,
                value,
            )
        )
    for base in REGIMES.values():
        top = [
            r.headline_share for r in result if r.base_period == base and r.parent_id == headline
        ]
        if not 0.985 <= sum(top) <= 1.015:
            raise SourceLayoutError(
                f"Headline expenditure weights fail roundoff tolerance: {base}, {sum(top)}"
            )
    return result


def export_validation_workbook(
    path: str, weights: list[BaseWeight], catalog: dict[str, dict[str, Any]]
) -> None:
    """Create a reproducible analyst review workbook; never modify official figures."""
    book = openpyxl.Workbook()
    index = book.active
    assert index is not None
    index.title = "index_catalog"
    index.append(["series_id", "name", "frequency", "source"])
    for sid, entry in sorted(catalog.items()):
        index.append([sid, entry["name"], entry["frequency"], entry["source_url"]])
    sheet = book.create_sheet("base_weights")
    sheet.append(
        [
            "series_id",
            "parent_id",
            "base_period",
            "headline_share",
            "parent_share",
            "official_label",
        ]
    )
    for item in sorted(weights, key=lambda w: (w.base_period, w.series_id)):
        sheet.append(
            [
                item.series_id,
                item.parent_id,
                item.base_period.isoformat(),
                item.headline_share,
                item.parent_share,
                item.label,
            ]
        )
    book.save(path)
