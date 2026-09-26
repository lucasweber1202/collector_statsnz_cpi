"""Download the official Stats NZ Consumers Price Index release CSV."""

from __future__ import annotations

import calendar
import csv
import hashlib
import html
import io
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from scripts.config import REQUEST_TIMEOUT, USER_AGENT
from scripts.time_series import Observation

logger = logging.getLogger(__name__)
COUNTRY_CURRENCY = "NZD"
SOURCE_ROOT = "https://www.stats.govt.nz"
MAX_STALE_MONTHS = 6
MIN_HISTORY_YEARS = 3
FIELDS = {"Series_reference", "Period", "Data_value", "STATUS", "UNITS", "Group", "Series_title_1", "Series_title_2", "Subject"}


@dataclass(frozen=True)
class SourceData:
    observations: list[Observation]
    catalog: dict[str, dict[str, Any]]


def build_series_id(native: str) -> str:
    """Preserve the native dotted code in a parseable ID."""
    if not re.fullmatch(r"[A-Z0-9]+(?:\.[A-Z0-9]+)+", native):
        raise ValueError(f"Invalid native code: {native}")
    return "STATSNZ_CPI_" + native.replace(".", "_")


def parse_series_id(series_id: str) -> str:
    """Recover the precise Stats NZ code."""
    if not series_id.startswith("STATSNZ_CPI_"):
        raise ValueError(f"Invalid series_id: {series_id}")
    native = series_id.removeprefix("STATSNZ_CPI_").replace("_", ".")
    if build_series_id(native) != series_id:
        raise ValueError(f"Invalid series_id: {series_id}")
    return native


def discover_csv(client: httpx.Client, today: date) -> tuple[str, date, str]:
    """Find the latest quarterly index-level CSV, not a hardcoded release."""
    for offset in range(5):
        index = today.year * 12 + today.month - 1 - offset * 3
        year, month0 = divmod(index, 12)
        quarter_month = (month0 // 3 + 1) * 3
        page = f"{SOURCE_ROOT}/information-releases/consumers-price-index-{calendar.month_name[quarter_month].lower()}-{year}-quarter/"
        response = client.get(page)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        markup = html.unescape(response.text)
        match = re.search(r'"DocumentLink":"([^"]+-index-numbers\.csv)"', markup, re.IGNORECASE)
        published = re.search(r'"PublicationDate":"(\d{4}-\d{2}-\d{2})', markup)
        if not match or not published:
            raise ValueError(f"Stats NZ release layout changed: {page}")
        return urljoin(SOURCE_ROOT, match.group(1).replace("\\/", "/")), date.fromisoformat(published.group(1)), page
    raise ValueError("No recent official CPI release found")


def parse_csv(blob: bytes, url: str, published: date) -> SourceData:
    """Parse finite index levels, native metadata, and quarterly period ends."""
    reader = csv.DictReader(io.StringIO(blob.decode("utf-8-sig")))
    if not reader.fieldnames or not FIELDS.issubset(reader.fieldnames):
        raise ValueError("CPI CSV header changed")
    catalog: dict[str, dict[str, Any]] = {}
    observations: list[Observation] = []
    snapshot = hashlib.sha256(blob).hexdigest()
    for row in reader:
        group = row["Group"]
        if row["UNITS"] != "Index" or row["Subject"] != "CPI" or not group.startswith("CPI "):
            continue
        sid = build_series_id(row["Series_reference"])
        titles = [v for key in ("Series_title_1", "Series_title_2") if (v := row[key]) not in ("", "NA")]
        descriptor = {"name": " / ".join(titles) or row["Series_reference"],
                      "description": f"Stats NZ CPI {group}; official code {row['Series_reference']}",
                      "country": COUNTRY_CURRENCY, "frequency": "quarterly", "unit": "index",
                      "eco_group": "consumer_prices", "source_url": url,
                      "last_publish_date": published}
        if sid in catalog and catalog[sid] != descriptor:
            raise ValueError(f"Metadata conflict: {sid}")
        catalog[sid] = descriptor
        try:
            year, month = map(int, row["Period"].split("."))
            if month not in (3, 6, 9, 12):
                raise ValueError("Not a quarter end")
            ref = date(year, month, calendar.monthrange(year, month)[1])
        except ValueError as exc:
            raise ValueError(f"Invalid CPI period: {row['Period']}") from exc
        raw = row["Data_value"]
        if raw in ("", "NA"):
            continue
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid CPI value {raw!r} for {sid}") from exc
        if not math.isfinite(value) or value < 0 or row["STATUS"] not in {"FINAL", "REVISED", "PROVISIONAL"}:
            raise ValueError(f"Invalid CPI value/status for {sid} on {ref}")
        if value > 0:  # Official historical zero placeholders denote unavailable periods.
            observations.append(Observation(sid, ref, value, snapshot))
    if not observations or not any(o.series_id == "STATSNZ_CPI_CPIQ_SE9A" for o in observations):
        raise ValueError("Official all-groups CPI observations missing")
    observed = {o.series_id for o in observations}
    return SourceData(observations, {sid: v for sid, v in catalog.items() if sid in observed})


def filter_usable_series(data: SourceData, today: date) -> SourceData:
    """Remove dead or short histories using only non-null observations."""
    dates: dict[str, list[date]] = {}
    for obs in data.observations:
        dates.setdefault(obs.series_id, []).append(obs.reference_date)
    keep: set[str] = set()
    for sid, points in dates.items():
        first, last = min(points), max(points)
        stale = (today.year - last.year) * 12 + today.month - last.month
        history = (last.year - first.year) * 12 + last.month - first.month
        if stale <= MAX_STALE_MONTHS and history >= MIN_HISTORY_YEARS * 12:
            keep.add(sid)
        else:
            logger.info("Dropped %s: stale=%d months, history=%d months", sid, stale, history)
    if not keep:
        raise ValueError("No usable CPI series")
    return SourceData([o for o in data.observations if o.series_id in keep], {s: v for s, v in data.catalog.items() if s in keep})


def collect() -> SourceData:
    """Retrieve the current official CSV and filter series before storage."""
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        url, published, page = discover_csv(client, datetime.now(UTC).date())
        response = client.get(url)
        response.raise_for_status()
    parsed = parse_csv(response.content, url, published)
    logger.info("%s: %d candidate series and %d observations", page, len(parsed.catalog), len(parsed.observations))
    return filter_usable_series(parsed, datetime.now(UTC).date())
