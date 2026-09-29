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

# Canonical metadata vocabulary produced by this source.
FREQUENCIES: frozenset[str] = frozenset({"quarterly"})
UNITS: frozenset[str] = frozenset({"index"})
ECO_GROUPS: frozenset[str] = frozenset({"consumer_prices"})


logger = logging.getLogger(__name__)
COUNTRY_CURRENCY = "NZD"
SOURCE_ROOT = "https://www.stats.govt.nz"
MAX_STALE_MONTHS = 6
MIN_HISTORY_YEARS = 3
MIN_PAYLOAD_BYTES = {"csv": 100_000, "xlsx": 20_000}
MIN_SOURCE_ROWS = 20_000
FIELDS = {
    "Series_reference",
    "Period",
    "Data_value",
    "STATUS",
    "UNITS",
    "Group",
    "Series_title_1",
    "Series_title_2",
    "Subject",
}


class SourceLayoutError(ValueError):
    """The official release page or file no longer has the audited layout."""


class SourceAccessError(RuntimeError):
    """The source answered with something other than the requested file."""


@dataclass(frozen=True)
class Release:
    """Official identity of one quarterly CPI release, taken from its page."""

    page_url: str
    published: date
    csv_url: str
    workbook_url: str


@dataclass(frozen=True)
class SourceData:
    observations: list[Observation]
    catalog: dict[str, dict[str, Any]]
    release: Release | None = None
    workbook: bytes = b""
    source_catalog: dict[str, dict[str, Any]] | None = None
    source_observations: list[Observation] | None = None


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


def discover_release(client: httpx.Client, today: date) -> Release:
    """Find the latest quarterly release page and its official download links."""
    for offset in range(5):
        index = today.year * 12 + today.month - 1 - offset * 3
        year, month0 = divmod(index, 12)
        quarter_month = (month0 // 3 + 1) * 3
        page = f"{SOURCE_ROOT}/information-releases/consumers-price-index-{calendar.month_name[quarter_month].lower()}-{year}-quarter/"
        response = client.get(page)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        return parse_release_page(response.text, page)
    raise SourceAccessError("No recent official CPI release found")


def parse_release_page(markup: str, page: str) -> Release:
    """Read the publication date and the two audited files from a release page."""
    text = html.unescape(markup)
    csv_link = re.search(r'"DocumentLink":"([^"]+-index-numbers\.csv)"', text, re.IGNORECASE)
    book_link = re.search(r'"DocumentLink":"([^"]+-quarter\.xlsx)"', text, re.IGNORECASE)
    published = re.search(r'"PublicationDate":"(\d{4}-\d{2}-\d{2})', text)
    if not csv_link or not book_link or not published:
        raise SourceLayoutError(f"Stats NZ release layout changed: {page}")
    return Release(
        page,
        date.fromisoformat(published.group(1)),
        urljoin(SOURCE_ROOT, csv_link.group(1).replace("\\/", "/")),
        urljoin(SOURCE_ROOT, book_link.group(1).replace("\\/", "/")),
    )


def discover_csv(client: httpx.Client, today: date) -> tuple[str, date, str]:
    """Backward-compatible view of the release: CSV URL, publication date, page."""
    release = discover_release(client, today)
    return release.csv_url, release.published, release.page_url


def check_payload(response: httpx.Response, kind: str) -> bytes:
    """Refuse an HTML challenge or error page before it can be parsed as data."""
    response.raise_for_status()
    blob = response.content
    content_type = response.headers.get("content-type", "").lower()
    head = blob[:512].lstrip().lower()
    if "text/html" in content_type or head.startswith((b"<!doctype", b"<html")):
        raise SourceAccessError(f"Stats NZ returned HTML instead of {kind}: {response.url}")
    if kind == "xlsx" and not blob.startswith(b"PK\x03\x04"):
        raise SourceAccessError(f"Stats NZ workbook is not an XLSX file: {response.url}")
    if kind == "csv" and not blob.removeprefix(b"\xef\xbb\xbf").lstrip(b'"').startswith(
        b"Series_reference"
    ):
        raise SourceLayoutError(
            f"Stats NZ CSV does not start with the audited header: {response.url}"
        )
    if len(blob) < MIN_PAYLOAD_BYTES[kind]:
        raise SourceAccessError(f"Stats NZ {kind} is implausibly small ({len(blob)} bytes)")
    return blob


def parse_csv(
    blob: bytes, url: str, published: date, min_rows: int = MIN_SOURCE_ROWS
) -> SourceData:
    """Parse finite index levels, native metadata, and quarterly period ends."""
    reader = csv.DictReader(io.StringIO(blob.decode("utf-8-sig")))
    if not reader.fieldnames or not FIELDS.issubset(reader.fieldnames):
        raise SourceLayoutError("CPI CSV header changed")
    catalog: dict[str, dict[str, Any]] = {}
    observations: list[Observation] = []
    seen: set[tuple[str, date]] = set()
    snapshot = hashlib.sha256(blob).hexdigest()
    rows = 0
    for row in reader:
        rows += 1
        group = row["Group"]
        if row["UNITS"] != "Index" or row["Subject"] != "CPI" or not group.startswith("CPI "):
            continue
        sid = build_series_id(row["Series_reference"])
        titles = [
            v for key in ("Series_title_1", "Series_title_2") if (v := row[key]) not in ("", "NA")
        ]
        descriptor = {
            "name": " / ".join(titles) or row["Series_reference"],
            "description": f"Stats NZ CPI {group}; official code {row['Series_reference']}",
            "country": COUNTRY_CURRENCY,
            "frequency": "quarterly",
            "unit": "index",
            "eco_group": "consumer_prices",
            "source_url": url,
            "last_publish_date": published,
        }
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
        if (
            not math.isfinite(value)
            or value < 0
            or row["STATUS"] not in {"FINAL", "REVISED", "PROVISIONAL"}
        ):
            raise ValueError(f"Invalid CPI value/status for {sid} on {ref}")
        if (sid, ref) in seen:
            raise ValueError(f"Duplicate CPI economic key: {sid} {ref}")
        seen.add((sid, ref))
        if value > 0:  # Official historical zero placeholders denote unavailable periods.
            observations.append(Observation(sid, ref, value, snapshot))
    if not observations or not any(o.series_id == "STATSNZ_CPI_CPIQ_SE9A" for o in observations):
        raise SourceLayoutError("Official all-groups CPI observations missing")
    if rows < min_rows:
        raise SourceLayoutError(f"CPI CSV has {rows} rows; expected at least {min_rows}")
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
    return SourceData(
        [o for o in data.observations if o.series_id in keep],
        {s: v for s, v in data.catalog.items() if s in keep},
    )


def collect() -> SourceData:
    """Retrieve the current release's CSV and weight workbook, then filter series.

    ``catalog``/``observations`` are the filtered series written to metadata and
    time_series. ``source_catalog``/``source_observations`` keep the complete
    parsed release, because official weights, the hierarchy and the validation
    describe series that the freshness filter may drop.
    """
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        release = discover_release(client, datetime.now(UTC).date())
        blob = check_payload(client.get(release.csv_url), "csv")
        workbook = check_payload(client.get(release.workbook_url), "xlsx")
    parsed = parse_csv(blob, release.csv_url, release.published)
    usable = filter_usable_series(parsed, datetime.now(UTC).date())
    logger.info(
        "%s: %d source series, %d usable, %d observations",
        release.page_url,
        len(parsed.catalog),
        len(usable.catalog),
        len(usable.observations),
    )
    return SourceData(
        usable.observations, usable.catalog, release, workbook, parsed.catalog, parsed.observations
    )
