"""Collect official Stats NZ Consumers Price Index."""

from __future__ import annotations

import argparse
import io
import logging
import sys
import traceback
from datetime import UTC, date, datetime

from sqlalchemy.engine import Engine

from scripts.config import (
    DEFAULT_START_DATE,
    LOG_LEVEL,
    START_DATE_LOOKBACK_MONTHS,
    missing_environment,
)
from scripts.db import build_engine
from scripts.extract import SourceData, SourceLayoutError, collect
from scripts.init_db import init_db
from scripts.metadata import upsert_metadata
from scripts.original_weights import build_hierarchy, upsert_hierarchy, upsert_original_weights
from scripts.releases import LAYOUT_CHANGED, classify_release, stored_release
from scripts.run_logs import insert_run_log
from scripts.time_series import get_last_observations, upsert_time_series
from scripts.validate import validate_release
from scripts.weight_sources import parse_base_weights

logger = logging.getLogger("main")


def _setup_logging(level: str) -> io.StringIO:
    """Capture collector messages for the run log."""
    buffer = io.StringIO()
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(formatter)
    buffer_handler = logging.StreamHandler(stream=buffer)
    buffer_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.ERROR)
    root.addHandler(stream_handler)
    root.addHandler(buffer_handler)
    logging.getLogger("main").setLevel(level.upper())
    logging.getLogger("scripts").setLevel(level.upper())
    return buffer


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect the official Stats NZ quarterly Consumers Price Index."
    )
    parser.add_argument("--log-level", default=LOG_LEVEL)
    parser.add_argument("--start-date", type=date.fromisoformat, default=None)
    return parser.parse_args(argv)


def _start_date(engine: Engine, explicit: date | None) -> date:
    if explicit:
        return explicit
    last = get_last_observations(engine)
    if not last:
        return DEFAULT_START_DATE
    latest = max(last.values())
    month_index = latest.year * 12 + latest.month - 1 - START_DATE_LOOKBACK_MONTHS
    year, zero_month = divmod(month_index, 12)
    return date(year, zero_month + 1, 1)


def main(args: argparse.Namespace) -> int:
    """Run source extraction and idempotent writes."""
    missing = missing_environment()
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    engine = build_engine()
    try:
        init_db(engine)
        start = _start_date(engine, args.start_date)
        try:
            data = collect()
            assert data.release is not None and data.source_catalog is not None
            assert data.source_observations is not None
            full = SourceData(
                data.source_observations, data.source_catalog, data.release, data.workbook
            )
            weights = parse_base_weights(data.workbook, data.source_catalog)
        except SourceLayoutError:
            logger.error("release_status=%s", LAYOUT_CHANGED)
            raise
        # Validation reads the complete release; the freshness filter only
        # decides which index series are written to time_series/metadata.
        validate_release(full, weights)
        observations = [item for item in data.observations if item.reference_date >= start]
        if not observations:
            raise ValueError(f"CPI source has no observations since {start}")
        collected_at = datetime.now(UTC)
        with engine.begin() as conn:
            previous = stored_release(conn, sorted(data.catalog))
            result = upsert_time_series(conn, observations, collected_at)
            inserted, updated = upsert_metadata(conn, data.catalog, collected_at)
            weight_result = upsert_original_weights(conn, weights, collected_at)
            hierarchy_inserted, hierarchy_updated = upsert_hierarchy(
                conn, build_hierarchy(data.source_catalog), collected_at
            )
            status = classify_release(
                previous,
                data.release.published,
                max(item.reference_date for item in observations),
                len(result.written_keys)
                + weight_result.new_weights
                + weight_result.new_vintages
                + weight_result.same_day_updates,
            )
        logger.info(
            "release_status=%s published=%s page=%s",
            status,
            data.release.published,
            data.release.page_url,
        )
        logger.info(
            "observations=%d new=%d revised=%d same_day=%d metadata_inserted=%d metadata_updated=%d",
            len(observations),
            result.new_observations,
            result.new_vintages,
            result.same_day_updates,
            inserted,
            updated,
        )
        logger.info(
            "weights=%s hierarchy_inserted=%d hierarchy_updated=%d",
            weight_result,
            hierarchy_inserted,
            hierarchy_updated,
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    log_buffer = _setup_logging(args.log_level)
    started_at = datetime.now(UTC)
    status = "success"
    tb_text: str | None = None
    return_code = 0
    try:
        return_code = main(args)
    except Exception:
        status = "error"
        tb_text = traceback.format_exc()
        logging.getLogger("main").exception("Pipeline failed")
        return_code = 1
    finally:
        finished_at = datetime.now(UTC)
        try:
            engine = build_engine()
            init_db(engine)
            insert_run_log(
                engine,
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                log_text=log_buffer.getvalue(),
                traceback_text=tb_text,
            )
            engine.dispose()
        except Exception:
            logging.getLogger("main").exception("Could not persist run log")
    if return_code != 0:
        raise SystemExit(return_code)
