"""Export CPI native index catalogue and official expenditure base weights."""
from __future__ import annotations

import argparse
import html
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx

from scripts.config import REQUEST_TIMEOUT, USER_AGENT
from scripts.extract import SOURCE_ROOT, discover_csv, parse_csv
from scripts.weight_sources import export_validation_workbook, parse_base_weights


def main() -> None:
    parser = argparse.ArgumentParser(description="Export official Stats NZ CPI validation workbook.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        csv_url, published, page = discover_csv(client, datetime.now(UTC).date())
        response = client.get(page)
        response.raise_for_status()
        match = re.search(r'"DocumentLink":"([^"]+quarter\.xlsx)"', html.unescape(response.text))
        if match is None:
            raise ValueError("Official CPI summary workbook link missing")
        book_url = urljoin(SOURCE_ROOT, match.group(1).replace("\\/", "/"))
        csv_response = client.get(csv_url)
        book_response = client.get(book_url)
        csv_response.raise_for_status()
        book_response.raise_for_status()
    data = parse_csv(csv_response.content, csv_url, published)
    weights = parse_base_weights(book_response.content, data.catalog)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    export_validation_workbook(str(args.output), weights, data.catalog)


if __name__ == "__main__":
    main()
