# Stats NZ quarterly CPI target collector

Standalone Python 3.11 collector of the official Stats NZ quarterly CPI: all
groups plus every published group, subgroup and class index level, the official
Table 8 base expenditure weights and the CPI hierarchy. Writes `metadata`,
`time_series`, `logs`, `original_weights` and `cpi_hierarchy` in schema
`collector_statsnz_cpi`.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
COLLECTOR_DB_URL=postgresql+psycopg2://user:password@localhost:5432/database .venv/bin/python main.py
```

Production uses `PROD=true` and the usual Databricks/Key Vault settings.

Tests: `pytest` (Spark grammar needs `java`). Add
`COLLECTOR_TEST_PG_URL=<disposable PostgreSQL URL>` for the PostgreSQL write
paths and `CPI_LIVE_SMOKE=1` for the live official release check.

See `METHODOLOGY.md` for source, validation, release monitoring and
point-in-time rules. Analyst workbook:
`python -m scripts.export_validation_xlsx --output validation.xlsx`.
