"""Export CPI native index catalogue and official expenditure base weights."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import httpx

from scripts.config import REQUEST_TIMEOUT, USER_AGENT
from scripts.extract import check_payload, discover_release, parse_csv
from scripts.weight_sources import export_validation_workbook, parse_base_weights


def main() -> None:
    parser = argparse.ArgumentParser(description="Export official Stats NZ CPI validation workbook.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        release = discover_release(client, datetime.now(UTC).date())
        csv_blob = check_payload(client.get(release.csv_url), "csv")
        book_blob = check_payload(client.get(release.workbook_url), "xlsx")
    data = parse_csv(csv_blob, release.csv_url, release.published)
    weights = parse_base_weights(book_blob, data.catalog)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    export_validation_workbook(str(args.output), weights, data.catalog)


if __name__ == "__main__":
    main()
