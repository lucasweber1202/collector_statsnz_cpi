"""Effective parent shares from official Table 8 baskets and published indices.

Each basket percentage is preserved unmodified in original_weights. For the
quarter's movement use opening shares: w_i(b) * I_i(t-1)/I_i(b), normalized
within each complete parent. The new basket applies strictly after its price
reference quarter. A sole child inherits its parent's full share. Coverage begins at
the earliest published basket (June 2014); no earlier weights are fabricated.
"""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date

from scripts.extract import SourceData, SourceLayoutError
from scripts.weight_sources import BaseWeight, parent_id
from scripts.weights import WeightObservation

logger = logging.getLogger(__name__)
PREFIX = "STATSNZ_CPI_CPIQ_"
HEADLINE = PREFIX + "SE9A"


def derive_weights(data: SourceData, originals: list[BaseWeight]) -> list[WeightObservation]:
    """Store opening-quarter shares, without renormalizing incomplete parents."""
    values = {(o.series_id, o.reference_date): o.value for o in data.observations}
    periods = sorted({o.reference_date for o in data.observations if o.series_id == HEADLINE})
    regimes = sorted({w.base_period for w in originals})
    source = {(w.series_id, w.base_period): w.percent for w in originals}
    children: dict[str, list[str]] = defaultdict(list)
    for sid in data.catalog:
        parent = parent_id(sid.removeprefix(PREFIX))
        if parent is not None:
            children[parent].append(sid)
    output = [WeightObservation(HEADLINE, t, 1.0, "official-basket") for t in periods]
    omitted: dict[str, int] = defaultdict(int)
    for when in periods:
        applicable = [b for b in regimes if b < when]
        if not applicable:
            continue
        base = max(applicable)
        month_index = when.year * 12 + when.month - 1 - 3
        year, zero_month = divmod(month_index, 12)
        previous = date(year, zero_month + 1, calendar.monthrange(year, zero_month + 1)[1])
        for parent, kids in children.items():
            active = [sid for sid in kids if (sid, when) in values and (sid, previous) in values]
            if not active:
                continue
            if len(kids) == 1:
                output.append(WeightObservation(active[0], when, 1.0, "inherited-sole-child"))
                continue
            required = [sid for sid in kids if source.get((sid, base), 0) > 0]
            if any(sid not in active for sid in required):
                omitted["missing-component-index"] += 1
                continue
            stated = source.get((parent, base))
            total_original = sum(source.get((sid, base), 0) for sid in kids)
            if stated is None or abs(total_original - stated) > 0.005 * (len(kids) + 1) + 1e-9:
                omitted["incomplete-official-basket"] += 1
                continue
            missing_bases = [
                sid
                for sid in active
                if source.get((sid, base), 0) > 0 and not values.get((sid, base))
            ]
            if missing_bases:
                omitted["unpublished-base-index"] += 1
                continue
            contributions: list[tuple[str, float]] = []
            for sid in active:
                weight = source.get((sid, base))
                if weight is None:
                    # A component that only existed in an older regime must
                    # not inherit an arbitrary newer weight.
                    continue
                if weight == 0:
                    contributions.append((sid, 0.0))
                    continue
                index_base = values.get((sid, base))
                if index_base is None or index_base <= 0:
                    raise SourceLayoutError(f"Weight base index missing: {sid} {base}")
                contributions.append((sid, weight * values[sid, previous] / index_base))
            total = sum(v for _, v in contributions)
            if total <= 0:
                raise SourceLayoutError(f"No positive component shares: {parent} {when}")
            output.extend(
                WeightObservation(sid, when, v / total, "price-updated-basket")
                for sid, v in contributions
            )
    logger.info(
        "Derived %d parent weight rows; official basket coverage starts %s", len(output), regimes[0]
    )
    if omitted:
        logger.warning(
            "Omitted incomplete parent/quarter systems (no partial normalization): %s",
            dict(omitted),
        )
    return output
