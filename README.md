# Auction Price Tracker

Automates the daily workflow of finding, pricing, and reporting on salvage vehicles from **Copart** and **IAAI** auctions.

---

## How it works

Each day the pipeline:
1. Scrapes today's matching lots from Copart and IAAI based on your filter definitions
2. Removes lots that already appeared yesterday (rescheduled auctions — Copart only)
3. Looks up final sale prices on **bidfax.info** for yesterday's lots
4. Retries any lots still showing "In Progress"
5. Aggregates everything into an Excel workbook
6. Generates a browsable HTML report

---

## Directory structure

```
.
├── run_daily.py            # Orchestrator — runs the full pipeline
│
├── scripts/                # Individual pipeline steps (CLI entry points)
│   ├── copart_search.py        # Scrapes Copart lots via internal API
│   ├── iaai_search.py          # Scrapes IAAI lots via browser automation
│   ├── remove_duplicates.py    # Removes rescheduled lots between days
│   ├── bidfax_info.py          # Looks up prices on bidfax.info
│   ├── price_refresh.py        # Retries In Progress prices across all CSVs
│   ├── price_fix.py            # Re-fetches bidfax data for specific lot numbers
│   ├── build_workbook.py       # Builds Excel workbook from price CSVs
│   ├── workbook_to_html.py     # Generates HTML report from workbook
│   └── bidcars_info.py         # Alternative price lookup via bid.cars
│
├── clients/                # External-system abstractions (swap real ⇄ fake in tests)
│   ├── bidfax.py               # BidfaxClient + BrowserBidfaxClient + FakeBidfaxClient
│   ├── copart.py               # CopartClient + HttpCopartClient + FakeCopartClient
│   └── iaai.py                 # IAAIClient + BrowserIAAIClient + FakeIAAIClient
│
├── core/                   # Shared pure-Python helpers (populated by future refactors)
│
├── tests/
│   ├── unit/                   # Fast tests on pure helpers (parsers, regex, etc.)
│   ├── integration/            # Script-level tests driven by Fake clients
│   ├── fixtures/csv/           # Trimmed CSVs used as test input
│   └── run_tests.py            # Stdlib test runner (no pytest required)
│
├── filters/                # Search filter definitions
│   ├── copart_filters.csv
│   └── iaai_filters.csv
│
├── caches/                 # Persistent lookup cache
│   └── bidfax_cache.json
│
├── logs/                   # Run logs
│   ├── processed_files.json   # Tracks which CSVs have been added to the workbook
│   └── bidfax_deletions.json  # Log of lots removed due to rescheduled auction
│
└── output/                 # All generated data files
    ├── copart_search_YYYY_MM_DD.csv
    ├── iaai_search_YYYY_MM_DD.csv
    ├── copart_price_YYYY_MM_DD.csv
    ├── iaai_price_YYYY_MM_DD.csv
    ├── auction_results.xlsx
    └── html_report/
```

---

## Requirements

```bash
pip install requests openpyxl beautifulsoup4 lxml nodriver
```

**Chrome or Chromium** must be installed — used by `nodriver` for browser automation on IAAI, bidfax.info, and Copart lot pages.

---

## Quick start

### 1. Configure filters

Edit `filters/copart_filters.csv` and `filters/iaai_filters.csv`.  
Each row defines one search. Supported fields:

| Field         | Required | Example                    |
|---------------|----------|----------------------------|
| `Make`        | Yes      | `Honda`                    |
| `Model`       | Yes      | `CR-V` (use `;` for multiple: `Corsair;Nautilus`) |
| `Year min`    | No       | `2023`                     |
| `Year max`    | No       | `2025`                     |
| `Odometer max`| No       | `30000`                    |
| `Fuel Type`   | No       | `Gas`, `Hybrid Engine`     |
| `Equipment`   | No       | `Touring` (post-filter on lot URL/title) |

Example row:
```
Make: Honda, Model: CR-V, Year min: 2023, Odometer max: 30000, Fuel Type: Hybrid Engine, Equipment: Touring
```

### 2. Run the full pipeline

```bash
python run_daily.py
```

Run from the project root. All output goes to the `output/` directory.

```bash
# Specify a different root if running from elsewhere
python run_daily.py --root /path/to/project
```

---

## Running individual scripts

Each script can be run standalone with sensible defaults.

### Search

```bash
python scripts/copart_search.py                          # uses filters/copart_filters.csv
python scripts/iaai_search.py                            # uses filters/iaai_filters.csv
```

### Deduplicate

```bash
# Remove yesterday's Copart lots that reappear in today's search
python scripts/remove_duplicates.py --auction copart
```

### Price lookup

```bash
# Yesterday's lots (default)
python scripts/bidfax_info.py --auction copart
python scripts/bidfax_info.py --auction iaai

# Specific date
python scripts/bidfax_info.py --auction copart --date 2026_04_07
```

### Refresh stale prices

```bash
# Retry all In Progress rows across all price CSVs
python scripts/price_refresh.py
```

### Fix specific lots

When a lot ends up with the wrong bidfax Link / Price / VIN (the initial search matched a different vehicle), re-run the lookup and overwrite every matching row across CSVs, workbook, and HTML report.

```bash
# One or more lot numbers, comma-separated
python scripts/price_fix.py --lots "44428368, 44428369, 44428360"

# Single lot, pointing at a specific directory
python scripts/price_fix.py --lots 44428368 --dir output

# Attach to an already-running Chrome (shared session)
python scripts/price_fix.py --lots 44428368 --browser-port 9222
```

Updates all three in-place:

- `output/<auction>_price_<date>.csv`  (every date, every match)
- `output/auction_results.xlsx`        (all sheets)
- `output/html_report/index.html`      (all tables)

Lots not found on bidfax.info are reported and skipped.

### Build workbook

```bash
python scripts/build_workbook.py --dir output/
```

### Generate HTML report

```bash
python scripts/workbook_to_html.py
python scripts/workbook_to_html.py --workbook output/auction_results.xlsx \
  --out output/html_report --search-dir output \
  --bidfax-cache caches/bidfax_cache.json
```

---

## Testing

```bash
python tests/run_tests.py                  # all tests
python tests/run_tests.py unit             # fast unit tests only
python tests/run_tests.py integration      # integration tests with Fake clients
python tests/run_tests.py -v               # verbose
```

Tests use the stdlib `unittest` runner — no pytest required. All browser-dependent scripts accept an optional `client=` parameter (e.g. `bidfax_info.process(..., client=FakeBidfaxClient(...))`) so tests run entirely in memory against canned responses. See `tests/integration/` for the patterns.

---

## Pipeline detail

Steps run in three phases. Steps within a phase execute in parallel; phases are sequential.

**Phase 1 — today's search scrapers (parallel)**

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `copart_search.py` | `filters/copart_filters.csv` | `output/copart_search_<today>.csv` |
| 2 | `iaai_search.py` | `filters/iaai_filters.csv` | `output/iaai_search_<today>.csv` |

**Phase 2 — dedup + IAAI pricing (parallel)**

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 3 | `remove_duplicates.py` | yesterday + today Copart CSVs | modifies yesterday's Copart search CSV in-place |
| 5 | `bidfax_info.py --auction iaai` | `output/iaai_search_<yesterday>.csv` | `output/iaai_price_<yesterday>.csv` |

**Phase 3 — Copart pricing, workbook, report (sequential)**

Steps 4, 6, 7, 8 share `bidfax_cache.json` and/or `auction_results.xlsx` and must run in order.

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 4 | `bidfax_info.py --auction copart` | `output/copart_search_<yesterday>.csv` | `output/copart_price_<yesterday>.csv` |
| 6 | `price_refresh.py` | all `output/*_price_*.csv` | updates price CSVs in-place |
| 7 | `build_workbook.py` | all new `output/*_price_*.csv` | `output/auction_results.xlsx` |
| 8 | `workbook_to_html.py` | `output/auction_results.xlsx` | `output/html_report/` |

---

## Notes

- **Shared Chrome session** — `run_daily.py` launches one Chrome instance at startup and passes its CDP port (`--browser-port`) to every browser-dependent step. Chrome is terminated after the last step. Running a script standalone (without `--browser-port`) starts its own browser automatically.
- **Copart lots** are pre-checked on copart.com before bidfax lookup. Up to 5 lot pages are loaded in parallel. If a lot page does not show "Sale ended", the auction was rescheduled — the lot is removed from the search CSV and logged to `logs/bidfax_deletions.json`.
- **bidfax.info results are cached** in `caches/bidfax_cache.json`. Both lot-number lookups and VIN lookups share this file. Only confirmed final prices/URLs are stored — "In Progress" results are always retried.
- **`build_workbook.py` tracks processed files** in `logs/processed_files.json` so the same CSV is never imported twice.
- The HTML report groups vehicles by Make, with clickable links to either the bidfax.info result page (orange button) or the original auction page (blue button).
