"""Create the three canonical collector tables."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from scripts.config import SCHEMA_NAME
from scripts.db import build_engine

CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}"
CREATE_METADATA_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.metadata (
    series_id VARCHAR(200) NOT NULL,
    name VARCHAR(500) NOT NULL,
    description VARCHAR(2000),
    country VARCHAR(3) NOT NULL,
    frequency VARCHAR(20),
    unit VARCHAR(50),
    first_observation DATE,
    last_observation DATE,
    observation_count INTEGER NOT NULL,
    eco_group VARCHAR(250) NOT NULL,
    source_url VARCHAR(1000) NOT NULL,
    last_publish_date DATE NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    CONSTRAINT pk_metadata PRIMARY KEY (series_id)
)"""
CREATE_TIME_SERIES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.time_series (
    series_id VARCHAR(200) NOT NULL,
    reference_date DATE NOT NULL,
    vintage_date DATE NOT NULL,
    value {{double_type}} NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    CONSTRAINT pk_time_series PRIMARY KEY (series_id, reference_date, vintage_date)
)"""
CREATE_LOGS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP NOT NULL,
    status VARCHAR(20) NOT NULL,
    log_text VARCHAR(65535) NOT NULL,
    traceback VARCHAR(65535),
    CONSTRAINT pk_logs PRIMARY KEY (id)
)"""

# Approved target exception (as in the UK CPI target): the official Table 8
# base expenditure weights, stored unmodified, and the published hierarchy.
CREATE_ORIGINAL_WEIGHTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.original_weights (
    series_id VARCHAR(200) NOT NULL,
    reference_date DATE NOT NULL,
    vintage_date DATE NOT NULL,
    weight {{double_type}} NOT NULL,
    weight_base_year INTEGER NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    CONSTRAINT pk_original_weights PRIMARY KEY (series_id, reference_date, vintage_date)
)"""
CREATE_HIERARCHY_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.cpi_hierarchy (
    series_id VARCHAR(200) NOT NULL,
    native_code VARCHAR(50) NOT NULL,
    parent_id VARCHAR(200),
    level INTEGER NOT NULL,
    name VARCHAR(500) NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    CONSTRAINT pk_cpi_hierarchy PRIMARY KEY (series_id)
)"""


def init_db(engine: Engine) -> None:
    """Create the schema and tables idempotently."""
    double_type = "DOUBLE PRECISION" if engine.dialect.name == "postgresql" else "DOUBLE"
    with engine.begin() as conn:
        for statement in (
            CREATE_SCHEMA,
            CREATE_METADATA_TABLE,
            CREATE_TIME_SERIES_TABLE.format(double_type=double_type),
            CREATE_LOGS_TABLE,
            CREATE_ORIGINAL_WEIGHTS_TABLE.format(double_type=double_type),
            CREATE_HIERARCHY_TABLE,
        ):
            conn.execute(text(statement))


if __name__ == "__main__":
    init_db(build_engine())
