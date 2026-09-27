"""Parse every emitted SQL shape with the production Spark SQL grammar.

``pyspark`` is a required dependency of every collector in the fleet, so this is
not an opt-in gate. It needs a Java runtime for the parser's JVM; only a missing
``java`` executable skips it, and the skip says so. Grammar is not runtime: this
proves Databricks can parse the statements, not that a corporate warehouse
accepted them.
"""

from __future__ import annotations

import os
import re
import shutil

import pytest
from sqlalchemy.sql.elements import TextClause

from scripts import init_db, metadata, original_weights, releases, run_logs, time_series


def statements() -> list[str]:
    """Every SQL text the collector can emit, with each batch size shape."""
    emitted: list[str | TextClause] = [
        init_db.CREATE_SCHEMA,
        init_db.CREATE_METADATA_TABLE,
        init_db.CREATE_TIME_SERIES_TABLE.format(double_type="DOUBLE"),
        init_db.CREATE_LOGS_TABLE,
        init_db.CREATE_ORIGINAL_WEIGHTS_TABLE.format(double_type="DOUBLE"),
        init_db.CREATE_HIERARCHY_TABLE,
        metadata._SELECT_SQL,
        metadata._UPDATE_SQL,
        time_series._AGGREGATES_SQL,
        time_series._LATEST_SQL,
        time_series._MAX_REFERENCE_SQL,
        time_series._UPDATE_SQL,
        run_logs._INSERT_SQL,
        releases._STORED_SQL,
        original_weights._LATEST_WEIGHTS_SQL,
        original_weights._UPDATE_WEIGHT_SQL,
        original_weights._SELECT_HIERARCHY_SQL,
        original_weights._UPDATE_HIERARCHY_SQL,
    ]
    for count in (1, 3):
        emitted += [
            metadata._insert_statement(count),
            metadata._merge_statement(count),
            time_series._insert_statement(count),
            time_series._merge_statement(count),
            original_weights._insert_statement(original_weights._WEIGHTS, original_weights._WEIGHT_COLUMNS, count),
            original_weights._insert_statement(original_weights._HIERARCHY, original_weights._HIERARCHY_COLUMNS, count),
            original_weights.weight_merge_statement(count),
            original_weights.hierarchy_merge_statement(count),
        ]
    out = []
    for statement in emitted:
        sql = statement if isinstance(statement, str) else statement.text
        sql = sql.replace("IN :series_ids", "IN (NULL)")
        out.append(re.sub(r":[A-Za-z][A-Za-z_0-9]*", "NULL", sql))
    return out


def test_no_postgres_only_types_reach_databricks() -> None:
    for sql in statements():
        assert "DOUBLE PRECISION" not in sql
        assert "SERIAL" not in sql.upper()


@pytest.mark.skipif(shutil.which("java") is None, reason="Spark's SQL parser needs a Java runtime")
def test_all_sql_statements_parse() -> None:
    from pyspark.sql import SparkSession

    os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")
    spark = (
        SparkSession.builder.master("local[1]")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.ui.enabled", "false")
        .appName("sql-grammar")
        .getOrCreate()
    )
    try:
        parser = spark._jsparkSession.sessionState().sqlParser()
        parsed = 0
        for sql in statements():
            parser.parsePlan(sql)
            parsed += 1
        assert parsed == len(statements())
    finally:
        spark.stop()
