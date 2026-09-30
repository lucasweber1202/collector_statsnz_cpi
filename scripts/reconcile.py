"""Audit quarterly aggregation from stored tables only; never fit official weights.

The diagnostic carries the original basket, opening shares and published
relatives for each parent/quarter. Publication precision intervals are a
compatibility check, not a claim to recover the unpublished exact index.
"""

from __future__ import annotations

import argparse
import calendar
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text

from scripts.config import SCHEMA_NAME
from scripts.db import build_engine

LATEST_SQL = "SELECT series_id,reference_date,{measure} FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY series_id,reference_date ORDER BY vintage_date DESC,collected_at DESC) AS rn FROM {schema}.{table}) r WHERE rn=1"
HIERARCHY_SQL = "SELECT series_id,parent_id FROM {schema}.cpi_hierarchy"


def _level_interval(value: float, when: date) -> tuple[float, float]:
    """Pre-rebase history is unrounded; modern CPI is published in whole points."""
    epsilon = 0.0 if when <= date(2017, 6, 30) else 0.5
    return max(0.0, value - epsilon), value + epsilon


def reconcile(
    levels: dict[tuple[str, date], float],
    shares: dict[tuple[str, date], float],
    originals: dict[tuple[str, date], float],
    parents: dict[str, str | None],
) -> list[dict[str, Any]]:
    """Expose complete systems and omitted coverage with independent roundoff bounds."""
    children: dict[str, list[str]] = defaultdict(list)
    for sid, parent in parents.items():
        if parent is not None:
            children[parent].append(sid)
    bases = sorted({base for _, base in originals})
    result = []
    for (parent, when), parent_level in sorted(levels.items()):
        if parent not in children:
            continue
        mi = when.year * 12 + when.month - 1 - 3
        year, month = divmod(mi, 12)
        previous = date(year, month + 1, calendar.monthrange(year, month + 1)[1])
        available = [b for b in bases if b < when]
        record: dict[str, Any] = {
            "parent": parent,
            "quarter": when.isoformat(),
            "children": children[parent],
            "base": max(available).isoformat() if available else None,
        }
        kids = children[parent]
        if not available or (parent, previous) not in levels:
            record["status"] = "NO_PUBLIC_BASKET_OR_PREVIOUS_INDEX"
            result.append(record)
            continue
        base = max(available)
        if any(
            (sid, when) not in shares or (sid, when) not in levels or (sid, previous) not in levels
            for sid in kids
        ):
            record["status"] = "INCOMPLETE_STORED_SYSTEM"
            result.append(record)
            continue
        predicted = sum(
            shares[sid, when] * levels[sid, when] / levels[sid, previous] for sid in kids
        )
        published = parent_level / levels[parent, previous]
        record.update(
            predicted=predicted,
            published=published,
            residual=predicted - published,
            opening_shares=[shares[sid, when] for sid in kids],
            share_sum=sum(shares[sid, when] for sid in kids),
            child_indices=[levels[sid, when] for sid in kids],
            previous_child_indices=[levels[sid, previous] for sid in kids],
            base_child_indices=[levels.get((sid, base)) for sid in kids],
            official_percentages=[originals.get((sid, base)) for sid in kids],
            parent_index=parent_level,
            previous_parent_index=levels[parent, previous],
        )
        if abs(record["share_sum"] - 1.0) > 1e-12:
            raise ValueError(f"Stored shares do not close: {parent} {when}")
        if len(kids) == 1:
            lo, hi = _level_interval(levels[kids[0], when], when)
            prevlo, prevhi = _level_interval(levels[kids[0], previous], previous)
            low, high = lo / prevhi, hi / prevlo
        else:
            if any((sid, base) not in levels or (sid, base) not in originals for sid in kids):
                raise ValueError(
                    f"Stored derived shares lack original weights/base levels: {parent} {when}"
                )
            contributions = [
                originals[sid, base] * levels[sid, previous] / levels[sid, base] for sid in kids
            ]
            total = sum(contributions)
            if any(
                abs(shares[sid, when] - v / total) > 1e-10 for sid, v in zip(kids, contributions)
            ):
                raise ValueError(
                    f"Stored shares disagree with the official-basket formula: {parent} {when}"
                )
            numerator_low = numerator_high = denominator_low = denominator_high = 0.0
            for sid in kids:
                w = originals[sid, base]
                lo, hi = _level_interval(levels[sid, when], when)
                prevlo, prevhi = _level_interval(levels[sid, previous], previous)
                blo, bhi = _level_interval(levels[sid, base], base)
                # Each official weight is published to 0.01 percentage point.
                numerator_low += max(0, w - 0.005) * lo / bhi
                numerator_high += (w + 0.005) * hi / blo
                denominator_low += max(0, w - 0.005) * prevlo / bhi
                denominator_high += (w + 0.005) * prevhi / blo
            low, high = numerator_low / denominator_high, numerator_high / denominator_low
        parentlo, parenthi = _level_interval(parent_level, when)
        prevlo, prevhi = _level_interval(levels[parent, previous], previous)
        published_low, published_high = parentlo / prevhi, parenthi / prevlo
        compatible = high >= published_low and low <= published_high
        record.update(
            lower=low,
            upper=high,
            published_lower=published_low,
            published_upper=published_high,
            status="PUBLIC_PRECISION_COMPATIBLE" if compatible else "UNEXPLAINED_RESIDUAL",
        )
        result.append(record)
    return result


def stored_reconciliation() -> list[dict[str, Any]]:
    """Read latest vintages and hierarchy from the database, without downloading."""
    engine = build_engine()
    try:
        with engine.connect() as conn:
            data = {}
            for table, measure in (
                ("time_series", "value"),
                ("weights", "weight"),
                ("original_weights", "weight"),
            ):
                rows = conn.execute(
                    text(LATEST_SQL.format(measure=measure, schema=SCHEMA_NAME, table=table))
                ).mappings()
                data[table] = {
                    (str(row["series_id"]), row["reference_date"]): float(row[measure])
                    for row in rows
                }
            parents = {
                str(r["series_id"]): r["parent_id"]
                for r in conn.execute(text(HIERARCHY_SQL.format(schema=SCHEMA_NAME))).mappings()
            }
        return reconcile(data["time_series"], data["weights"], data["original_weights"], parents)
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile CPI using stored outputs only")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = stored_reconciliation()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    if any(r["status"] == "UNEXPLAINED_RESIDUAL" for r in results):
        raise ValueError("Unexplained stored-output aggregation residual; inspect the report")


if __name__ == "__main__":
    main()
