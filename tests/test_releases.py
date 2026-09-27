"""Release classification uses source evidence, never the run date."""

from __future__ import annotations

from datetime import date

import pytest

from scripts.releases import (
    FIRST_RELEASE,
    NEW_RELEASE,
    REVISED_SOURCE,
    SAME_RELEASE,
    ReleaseRegressionError,
    StoredRelease,
    classify_release,
)

STORED = StoredRelease(date(2026, 7, 21), date(2026, 6, 30))


def test_first_release() -> None:
    assert classify_release(None, date(2026, 7, 21), date(2026, 6, 30), 10) == FIRST_RELEASE


def test_rerun_of_the_same_release_is_not_new() -> None:
    assert classify_release(STORED, date(2026, 7, 21), date(2026, 6, 30), 0) == SAME_RELEASE


def test_changed_values_under_the_same_publication_are_a_revision() -> None:
    assert classify_release(STORED, date(2026, 7, 21), date(2026, 6, 30), 3) == REVISED_SOURCE


def test_later_publication_is_a_new_release() -> None:
    assert classify_release(STORED, date(2026, 10, 20), date(2026, 9, 30), 164) == NEW_RELEASE


def test_older_publication_fails_closed() -> None:
    with pytest.raises(ReleaseRegressionError):
        classify_release(STORED, date(2026, 4, 21), date(2026, 3, 31), 0)


def test_without_publication_date_the_covered_period_decides() -> None:
    undated = StoredRelease(None, date(2026, 6, 30))
    assert classify_release(undated, None, date(2026, 9, 30), 5) == NEW_RELEASE
    assert classify_release(undated, None, date(2026, 6, 30), 0) == SAME_RELEASE
    assert classify_release(undated, None, date(2026, 6, 30), 1) == REVISED_SOURCE
