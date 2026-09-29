# Stats NZ CPI target collector — methodology

Authority: `guimasuko/collector_template` main `4bc65765cedd9c14aec196cff382df6dfb318c77`
(AUD/NZD are part of its `metadata.country` vocabulary). This repository emits
`country = NZD`.

## Source

- [Stats NZ quarterly CPI release](https://www.stats.govt.nz/information-releases/consumers-price-index-june-2026-quarter/).
  `discover_release` probes the five most recent quarterly release pages and
  reads, from the page itself, the `PublicationDate`, the `*-index-numbers.csv`
  link and the release workbook `*-quarter.xlsx` link. No URL is hardcoded.
- Payload checks before parsing (`check_payload`): HTML/challenge pages are
  refused whatever their `Content-Type`; the workbook must start with the XLSX
  (`PK\x03\x04`) magic; the CSV must start with the audited `Series_reference`
  header; both must exceed a minimum size; the CSV must carry at least 20,000
  rows and every required column. A failure raises `SourceAccessError` or
  `SourceLayoutError` and nothing is written.

## Target and series

- 164 native IDs: all groups (`CPIQ.SE9A`), 11 groups, 44 subgroups, 108
  classes. `series_id = STATSNZ_CPI_<native with . → _>`, reversible by
  `parse_series_id`. Values are quarterly **index levels** (base June 2017
  quarter = 1000), not growth rates. Stats NZ's early zero placeholders are
  treated as missing.
- The freshness filter (≥3 years of history, last point ≤6 months old) drops
  one class, `CPIQ.SE904404` (removed in the 2024 basket review), from
  `time_series`/`metadata`. It stays in `cpi_hierarchy` and in validation.
- Default start date is 1914-01-01 so the full published history is stored.
- June 2026 release (published 2026-07-21): 163 stored series, 22,590
  observations, June 1914 – June 2026. All groups: 1327 (Dec 2025), 1339
  (Mar 2026), 1359 (Jun 2026).

## Official weights and hierarchy (persisted)

Release workbook Table 8 publishes base expenditure weights (percent of
all-groups expenditure) for four baskets, expressed in the prices of the June
2014, September 2017, June 2020 and December 2024 quarters. The December 2024
weights apply from the March 2025 quarter
([CPI review 2024](https://www.stats.govt.nz/methods/consumers-price-index-review-2024/)).

- `original_weights` — every published Table 8 cell, unmodified, in percent:
  `(series_id, reference_date = base quarter end, vintage_date)`, plus
  `weight_base_year`. 595 cells on the June 2026 release. The all-groups 100 is
  definitional, not published, and not stored. Vintage rules are the
  time-series rules (new vintage on a later-day change, same-day overwrite,
  no-op when unchanged).
- `cpi_hierarchy` — one row per published identity (164): native code, level
  (0–3), parent, source name. Parent comes from the native code; the level is
  cross-checked against the published group label.
- **Base weights are not effective quarterly aggregation weights.** In a
  quarter `t` after base `b`, a component's effective share is its base share
  price-updated by its own relative, `w_i(b)·I_i(t)/I_i(b)` renormalised. That
  is a derived quantity and is not stored.

## Validation (runs before any write; failure stops the run)

`scripts/validate.py`, on the complete parsed release:

| Check | Rule |
| --- | --- |
| Hierarchy | exactly one root (`SE9A`); each code's level agrees with its published group label; every parent is published |
| Duplicates / orphans | no duplicate `(series_id, base)`; no Table 8 code without an index series |
| Base periods | exactly the four published bases |
| Top-level sums | 11 groups sum to 100 ± 0.05 for every base (published 100.00/99.99/100.00/100.01) |
| Child sums | children never exceed their parent beyond rounding; a shortfall beyond rounding fails unless pinned (one: Recreation and culture, June 2014 basket, 1.13 pp for a component discontinued before the current release) |
| Weightless series | the 14 index series without a Table 8 row must each be their parent's only child (they inherit its weight) |
| Cross-file | 280 index levels in workbook Table 2.01 (groups, subgroups, all groups, five quarters) equal the CSV exactly |
| Aggregation | all groups rebuilt from the 11 group indices with the Table 8 weights (Lowe formula, linked at each base quarter) matches the published index within 0.1% |

Aggregation result on the June 2026 release: 48 quarters checked (Sep 2014 –
Jun 2026); maximum |error| 0.063% in 47 of them. September 2024 (still in the
June 2020 basket) rebuilds 0.134% high; the published material does not explain
it, so it is pinned to its measured value and any drift fails. This validates
the weights and hierarchy against the published index. It does **not**
reproduce the CPI exactly: Stats NZ aggregates unrounded elementary indices that
are not published, and published levels are rounded to whole points.

`python -m scripts.export_validation_xlsx --output validation.xlsx` writes an
analyst workbook (`index_catalog`, `base_weights`) from the live files.

## Release monitoring

`scripts/releases.py` classifies each run from source evidence (the release
page's publication date and the latest covered quarter) against what the
database held before the run, plus the rows the run changed: `first_release`,
`same_release`, `new_release`, `revised_source`, and `layout_changed` (logged
when extraction raises `SourceLayoutError`, before any write). Re-running
against the same release on another day is `same_release`; a publication date
that goes backwards fails the run. The status is written to the run log.

## Point in time

- `vintage_date` is the UTC collection date, never the reference or publication
  date. A first backfill is dated the day it was collected; it is not evidence
  of what was known historically.
- `metadata.last_publish_date` is the release's own publication date, kept apart
  from `collected_at`.
- Stats NZ publishes the current history only; earlier release snapshots are
  not available from this source, so no historical vintage is reconstructed.
  Later changes become new vintages; a same-day change overwrites that day's
  vintage (template rule). Revisions older than the 9-month look-back are not
  re-read on incremental runs.

## Verification (2026-09-27)

- PostgreSQL 16.13: live `main.py` run 1 wrote 22,590 observations, 163
  metadata rows, 595 weights, 164 hierarchy rows (`first_release`); run 2 wrote
  nothing and left every `collected_at` unchanged (`same_release`).
- `tests/test_postgres_integration.py` on PostgreSQL: init, idempotent rerun,
  later-day vintage, same-day overwrite, metadata MERGE with NULL in every
  nullable column, time-series and weight MERGE, hierarchy MERGE with a NULL
  parent, run-log NULL traceback and truncation, release classification.
- Every emitted SQL statement parses with the Spark SQL grammar (pyspark 4.1.1).
  **Databricks corporate runtime: not verified.**
- Spot checks against raw CSV rows (first, middle, last, random) for all
  groups, food, household energy, electricity and second-hand cars: exact.

## Masuko authority verification

Pinned authority: `guimasuko/collector_template@4bc65765cedd9c14aec196cff382df6dfb318c77`. Physical `.github/` and `.vscode/` paths are checked against Git blobs. `.gitignore` and `scripts/databricks_engine.py` have no physical path in the template tree; they are canonical fenced blocks in `GUIDELINES.md` sections 8.1 and 8.9. The guideline Git blob is `089fbbca6a2241d3f02777b82631fbf81d49f6e0`; the two derived file blobs are `f0d1368264d24d7959d3137d618930a06f33795e` and `73821f7a530ab5cca2f5313180d71c17173e6e59`. `tests/test_architecture.py` checks all local blobs on every run. For independent source derivation, check out the exact authority commit and run `MASUKO_TEMPLATE_DIR=/path/to/collector_template python -m pytest -q tests/test_architecture.py`. This checks the guideline blob, extracts both fenced blocks and checks their hashes.
