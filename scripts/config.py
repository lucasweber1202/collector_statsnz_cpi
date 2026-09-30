"""Runtime settings loaded from environment variables.

A `.env` file in the repo root is auto-loaded if present. Every setting can
be overridden by exporting the matching environment variable.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
_ENV_FILE = ROOT_DIR / ".env"

if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if value and key not in os.environ:
            os.environ[key] = value


SCHEMA_NAME = "collector_statsnz_cpi"
CATALOG_NAME = "macrobond_inhouse"

# When the pipeline runs without an explicit --start-date, we look back this
# many months from the latest reference_date already stored to catch any
# late-arriving revisions without re-downloading the full archive.
START_DATE_LOOKBACK_MONTHS = 9

# When PROD is true, build_engine() uses the corporate Databricks engine
# (see scripts/databricks_engine.py). Otherwise it builds a SQLAlchemy
# engine from COLLECTOR_DB_URL -- which must point at a local SQL DB you
# control.
PROD = os.getenv("PROD", "false").lower() in ("1", "true", "yes")

DATABASE_URL = os.getenv("COLLECTOR_DB_URL", "")

# The official all-groups series begins in the June 1914 quarter; the default
# keeps the full published history rather than the template's 1960 floor.
DEFAULT_START_DATE = date.fromisoformat(os.getenv("COLLECTOR_START_DATE", "1914-01-01"))

REQUEST_TIMEOUT = float(os.getenv("COLLECTOR_HTTP_TIMEOUT", "30"))
DOWNLOAD_DELAY = float(os.getenv("COLLECTOR_DOWNLOAD_DELAY", "1.0"))
MAX_RETRIES = int(os.getenv("COLLECTOR_MAX_RETRIES", "3"))
BACKOFF_FACTOR = float(os.getenv("COLLECTOR_BACKOFF_FACTOR", "2.0"))
USER_AGENT = os.getenv(
    "COLLECTOR_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36",
)

LOG_LEVEL = os.getenv("COLLECTOR_LOG_LEVEL", "INFO")

# Databricks / Azure Key Vault settings (PROD only).
# DATABRICKS_TOKEN can be set directly to skip the Key Vault lookup;
# otherwise the token is fetched from AKV at engine-build time.
DBX_SERVER_HOSTNAME = os.getenv("DBX_SERVER_HOSTNAME", "")
DBX_HTTP_PATH = os.getenv("DBX_HTTP_PATH", "")
AKV_VAULT_URL = os.getenv("AKV_VAULT_URL", "")
AKV_SECRET_NAME = os.getenv("AKV_SECRET_NAME", "databricks-token")

# -- Source-specific constants below --------------------------------------
METADATA_TABLE = "metadata"
TIME_SERIES_TABLE = "time_series"
LOGS_TABLE = "logs"
ORIGINAL_WEIGHTS_TABLE = "original_weights"
HIERARCHY_TABLE = "cpi_hierarchy"
COUNTRY_CURRENCY = "NZD"


def missing_environment(prod: bool = PROD) -> list[str]:
    if not prod:
        return [] if DATABASE_URL else ["COLLECTOR_DB_URL"]
    required = {"DBX_SERVER_HOSTNAME": DBX_SERVER_HOSTNAME, "DBX_HTTP_PATH": DBX_HTTP_PATH}
    return sorted(name for name, value in required.items() if not value)

# Forecast-target release monitoring: a normal timeout still writes a success log.
POLL_INTERVAL = float(os.getenv("COLLECTOR_POLL_INTERVAL", "30"))
MAX_WAIT = float(os.getenv("COLLECTOR_MAX_WAIT", "900"))
if POLL_INTERVAL <= 0 or MAX_WAIT < 0:
    raise ValueError("Polling interval must be positive and maximum wait non-negative")

if MAX_RETRIES < 1 or BACKOFF_FACTOR < 0 or DOWNLOAD_DELAY < 0:
    raise ValueError("HTTP retry count must be positive and delays non-negative")
