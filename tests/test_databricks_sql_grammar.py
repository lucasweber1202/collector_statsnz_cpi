"""Parse every emitted SQL shape with the production Spark SQL grammar."""

from __future__ import annotations

import os
import re

import pytest
from sqlalchemy.sql.elements import TextClause

from scripts import init_db, metadata, run_logs, time_series


@pytest.mark.skipif(os.getenv("SPARK_GRAMMAR") != "1", reason="opt-in Spark JVM grammar")
def test_all_sql_statements_parse() -> None:
    from pyspark.sql import SparkSession

    os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")
    spark = SparkSession.builder.master("local[1]").config("spark.driver.bindAddress", "127.0.0.1").appName("rba-sql-grammar").getOrCreate()
    try:
        parser = spark._jsparkSession.sessionState().sqlParser()
        statements: list[str | TextClause] = [
            init_db.CREATE_SCHEMA,
            init_db.CREATE_METADATA_TABLE,
            init_db.CREATE_TIME_SERIES_TABLE.format(double_type="DOUBLE"),
            init_db.CREATE_LOGS_TABLE,
            metadata._SELECT_SQL,
            metadata._UPDATE_SQL,
            metadata._insert_statement(1),
            metadata._merge_statement(1),
            time_series._AGGREGATES_SQL,
            time_series._LATEST_SQL,
            time_series._MAX_REFERENCE_SQL,
            time_series._UPDATE_SQL,
            time_series._insert_statement(1),
            time_series._merge_statement(1),
            run_logs._INSERT_SQL,
        ]
        for statement in statements:
            sql = statement if isinstance(statement, str) else statement.text
            sql = sql.replace("IN :series_ids", "IN (NULL)")
            sql = re.sub(r":[A-Za-z][A-Za-z_0-9]*", "NULL", sql)
            parser.parsePlan(sql)
    finally:
        spark.stop()
