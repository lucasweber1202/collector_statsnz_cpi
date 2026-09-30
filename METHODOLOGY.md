# Stats NZ CPI target collector — methodology

Authority: `guimasuko/collector_template@4bc65765cedd9c14aec196cff382df6dfb318c77`, including `FORECAST_TARGET_GUIDELINES.md`. Country is `NZD`. Section 4 requires preserving official weights and documenting differences; sections 5 and 8 require reproducible derived measures and independent validation from stored outputs. They do not require exact recovery of unpublished elementary indices or a zero residual from rounded public indices.

## Sources and identities

The collector discovers the current quarterly release page and its publication date, index CSV and release XLSX. Payload signatures and required columns reject HTML challenges and changed layouts before writing. Native IDs are reversible (`CPIQ.SE9A` → `STATSNZ_CPI_CPIQ_SE9A`). Levels are quarterly CPI indices, June 2017 = 1000; early zero placeholders are missing values. The release has 164 identities: headline, 11 groups, 44 subgroups and 108 classes. The freshness filter retains 163 series; retired `SE904404` remains in the original hierarchy and weight history.

The June 2026 release, published 21 July 2026, contains 22,590 retained observations (June 1914–June 2026). The publication date is source evidence, not collection time.

## Official regimes and provenance

| Price reference quarter | Source | First movement using basket |
| --- | --- | --- |
| June 2014 | current release Table 8 | September 2014 |
| September 2017 | current Table 8, revised 2017 review | December 2017 |
| June 2020 | current Table 8 | September 2020 |
| June 2021 | corrected 2023 review workbook Table 1 | September 2021 |
| June 2022 | corrected 2023 review workbook Table 1 | September 2022 |
| June 2023 | corrected 2023 review workbook Table 1 | September 2023 |
| December 2024 | current Table 8 / 2024 review | March 2025 |

The pandemic adjustments changed international airfares and prepaid overseas accommodation, with rescaling of the remaining basket. Therefore the full interim table is retained, rather than changing only travel children. The 2024 review explicitly says the final annual adjustment was June 2023. No June 2024 interim basket is invented.

Official references:

- [2021 adjustment](https://www.stats.govt.nz/methods/impacts-of-covid-19-on-methodology-for-the-september-2021-quarter-cpi/)
- [2022 adjustment](https://www.stats.govt.nz/methods/price-index-methods-updates-for-the-september-2022-quarter/)
- [2023 methodology and corrected workbook](https://www.stats.govt.nz/methods/price-index-methods-updates-for-the-september-2023-quarter/)
- [Corrected workbook](https://www.stats.govt.nz/assets/Methods/Price-index-methods-updates-for-the-September-2023-quarter/consumers-price-index-reweight-2023-corrected.xlsx)
- [2017 revised review](https://www.stats.govt.nz/methods/consumers-price-index-review-2017-revised/)
- [2024 review](https://www.stats.govt.nz/methods/consumers-price-index-review-2024/)
- [CPI methodology / publication precision](https://datainfoplus.stats.govt.nz/item/nz.govt.stats/8b0860b8-cf63-4f12-a578-8eed8ba69ac3)

The interim parser validates the three dated headers, all 11 groups, hierarchy context, finite percentages and identities. Explicit name aliases bridge official label changes; an unknown identity fails. The retired package-holidays row has zero in all interim baskets and is omitted without inventing a native code. Download URL, table, periods and payload SHA-256 are written to run logs.

`original_weights` preserves 595 Table 8 cells plus 447 interim cells, in percentage points, with original price reference dates. Definitional all-groups 100 is not stored as a published cell. `cpi_hierarchy` preserves 164 native identities, levels, parents and names. Their persistence follows the same UTC collection-vintage rules as observations.

## Derived weights and boundaries

For the movement from quarter t−1 to t, choose the most recent basket b strictly before t. The opening share is:

`share_i(t) = [w_i(b) * I_i(t-1) / I_i(b)] / SUM_j[w_j(b) * I_j(t-1) / I_j(b)]`.

It weights the component relative `I_i(t)/I_i(t-1)`. These normalized shares are persisted separately in `weights`; originals are never overwritten or fitted to parents. A sole child inherits 1, and the headline has 1. At the basket's price reference quarter the previous basket still applies; the replacement starts the following quarter.

A parent is omitted if a positive-weight component lacks current/previous/base indices, or if the public children fail to account for the stated parent beyond source rounding. Remaining siblings are never silently normalized. Examples are the retired package-holidays component in the 2014 recreation basket and missing historic class base indices for passenger transport/accommodation. Freshness filtering cannot cause a parent to normalize away a removed positive-weight class. Omission counts and reasons are logged.

Derived basket coverage starts September 2014. Earlier CPI observations remain intact but do not acquire invented weights. Earlier historical baskets/classifications cannot be treated as the current hierarchy without the missing component indices and classification correspondence.

## Validation and stored-output audit

Before writing: hierarchy, duplicates/orphans, published base dates, 11-group sums, child sums, sole-child inheritance, workbook-versus-CSV cross-file checks and headline Lowe chain aggregation are validated. The existing 0.1% headline tolerance is unchanged. With interim regimes the old September 2024 exception is removed: all 48 quarters pass, maximum relative error 0.061840%.

After writing:

```bash
python -m scripts.export_validation_xlsx --output validation.xlsx
python -m scripts.reconcile --output reconciliation.json
```

Both commands read stored latest vintages only, without fetching source data. The reconciliation reports children, basket date, untouched percentages, base/current/previous levels, opening shares, predictions, published relatives and residuals. It independently checks the basket formula and sibling sums. Incomplete coverage remains explicitly classified.

The precision diagnostic uses official 0.01 percentage-point weights and whole-point indices after June 2017; historical pre-rebase levels are treated as unrounded. Conservative interval overlap establishes compatibility with public precision, not that rounding alone caused every discrepancy or that unpublished internal values have been recovered. An incompatible complete system raises an error. No weights or tolerances are calibrated against published parents.

On the 30 September 2026 stored-output run: 2,609 complete systems are public-precision compatible, 79 systems are incomplete and 5,176 parent-period records lack a supported public basket or previous index. Zero complete systems have unexplained residuals. Counts depend on the available release.

The previous material accommodation error (SE9096, June 2024) used an obsolete June 2020 regime. Using official June 2023 weights and opening shares gives 0.955224757803 versus published 0.955113636364, residual 0.000111121439 instead of 0.0580152619. This is case A: the implementation omitted official regimes and was corrected. Exact public reconstruction remains limited by publication precision and unavailable elementary inputs; this is documented as required by the authority, rather than imposing an additional zero-residual gate.

## Release monitoring and point in time

An empty database collects immediately. A populated database polls every 30 seconds, up to 900 seconds, as required by forecast guideline section 10. Within the five-month rewind, changed source values return immediately even if the latest quarter did not change. The previous implementation could miss such a revision while waiting for a new period. Unchanged timeout is a successful monitored run.

Scheduled one-shot invocation is `python main.py --no-watch`; an explicit `--start-date` is also one-shot. Release classifications distinguish first/same/new/revised source and layout change. A backwards publication date fails.

Collection timestamps are normalized to UTC naive before every write. Vintage is the UTC collection date. An unchanged rerun preserves collected_at; later-day changes insert a new vintage; same-day changes overwrite that day's vintage. As-of queries cannot see a future vintage. Current-source backfills do not reconstruct historical knowledge; incremental revision coverage is limited to the five-month rewind.

## Verification boundary

Python 3.11 fresh environment, declared dependencies, PostgreSQL 16.15, live collection/repeat/export, UTC and non-UTC sessions, revision/as-of tests and real Spark SQL parsing pass. Production engine routing is tested with substitutes. Corporate Databricks/AKV runtime remains **NOT_VERIFIED_CORPORATE**.

Verbatim files are compared against physical authority blobs or canonical GUIDELINES fenced blocks. `tests/test_architecture.py` can independently derive them using `MASUKO_TEMPLATE_DIR=/path/to/collector_template`. No authority file is modified by this repair.
