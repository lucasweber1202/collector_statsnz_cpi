# Stats NZ quarterly CPI target collector (work in progress)

Standalone Python 3.11 collector of official quarterly CPI index numbers, including the all-groups target and published group, subgroup, and class levels. It stores source-native IDs, historical index levels, vintages, metadata, and run logs using the canonical collector schema.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
COLLECTOR_DB_URL=postgresql+psycopg2://user:password@localhost:5432/database .venv/bin/python main.py
```

See `METHODOLOGY.md` for source, scope, and remaining forecast-target requirements. Production uses `PROD=true` and the usual Databricks/Key Vault settings.

The source base-weight/hierarchy validation export is available with `python -m scripts.export_validation_xlsx --output validation.xlsx`. It is an analyst review artifact, not yet a persisted `weights` table or a verified CPI aggregation.
