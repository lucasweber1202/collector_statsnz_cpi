"""Tell a new official release apart from a rerun, a revision or a broken layout.

The evidence is the source's own: the publication date it states for the file
(``metadata.last_publish_date``) and the latest period the file covers. Both are
compared with what the database already holds *before* this run writes, and the
number of rows this run actually changed tells a silent revision from a no-op.
The run date is never evidence: running twice on different days against the
same release is ``same_release``.

Statuses:

* ``first_release`` -- nothing from this source is stored yet;
* ``new_release`` -- the publication date advanced (or, for a source without
  one, the latest covered period advanced);
* ``revised_source`` -- same publication, but stored values changed or were
  added: the publisher replaced the file without a new release;
* ``same_release`` -- same publication, nothing changed;
* ``layout_changed`` -- the file failed its audited layout checks; set by
  ``main.py`` when extraction raises ``SourceLayoutError``, before any write.

A publication date that goes backwards fails the run: it means discovery picked
the wrong release, and writing it would move ``last_publish_date`` back in time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from scripts.config import METADATA_TABLE, SCHEMA_NAME

FIRST_RELEASE = "first_release"
NEW_RELEASE = "new_release"
REVISED_SOURCE = "revised_source"
SAME_RELEASE = "same_release"
LAYOUT_CHANGED = "layout_changed"

_STORED_SQL = text(
    f"SELECT COUNT(*) AS n, MAX(last_publish_date) AS published, MAX(last_observation) AS latest "
    f"FROM {SCHEMA_NAME}.{METADATA_TABLE} WHERE series_id IN :series_ids"
).bindparams(bindparam("series_ids", expanding=True))


class ReleaseRegressionError(ValueError):
    """The source states an older publication than the one already stored."""


@dataclass(frozen=True)
class StoredRelease:
    published: date | None
    latest_reference: date | None


def _as_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    assert isinstance(value, date)
    return value


def stored_release(conn: Connection, series_ids: list[str]) -> StoredRelease | None:
    """What the database held for these series before this run wrote anything."""
    if not series_ids:
        return None
    row = conn.execute(_STORED_SQL, {"series_ids": sorted(series_ids)}).mappings().one()
    if not row["n"]:
        return None
    return StoredRelease(_as_date(row["published"]), _as_date(row["latest"]))


def classify_release(
    previous: StoredRelease | None,
    published: date | None,
    latest_reference: date | None,
    changed_rows: int,
) -> str:
    """Classify one source file against the stored state and this run's writes."""
    if previous is None:
        return FIRST_RELEASE
    if published is not None and previous.published is not None:
        if published < previous.published:
            raise ReleaseRegressionError(
                f"Source publication {published} is older than stored {previous.published}"
            )
        if published > previous.published:
            return NEW_RELEASE
    elif (
        latest_reference is not None
        and previous.latest_reference is not None
        and latest_reference > previous.latest_reference
    ):
        return NEW_RELEASE
    return REVISED_SOURCE if changed_rows else SAME_RELEASE
