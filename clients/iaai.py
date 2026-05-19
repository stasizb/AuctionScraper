#!/usr/bin/env python3
"""
IAAIClient abstraction — wraps IAAI's public /Search JSON-over-HTML API.

  - IAAIClient         — the interface scripts depend on
  - SessionIAAIClient  — real implementation; takes a session pre-warmed
                         by clients.iaai_session (Playwright cookies)
  - FakeIAAIClient     — test double that returns canned row lists

Filter parsing (read_filters_csv / parse_filter_row) and the equipment
post-filter (equipment_matches) are pure helpers also exposed from this
module so the CLI wrapper stays thin.

Legacy nodriver-driven BrowserIAAIClient (and the dozens of async UI-
clicking helpers it depended on) lives in clients/iaai.py.bak — IAAI's
search panel was clicked filter-by-filter; now we POST one JSON payload
per filter row and parse the server-rendered HTML response. Saves a
browser tab per filter row, all bot-defense state shared via a single
Playwright cookie warmup.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dates       import normalize_auction_date
from core.concurrency import (  # noqa: F401  (re-exported)
    DEFAULT_TAB_CONCURRENCY,
    IAAI_TAB_CONCURRENCY,
)

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IAAI_BASE       = "https://www.iaai.com"
IAAI_SEARCH_URL = f"{IAAI_BASE}/Search"

# AUCTION_DATE_COL is sourced from core.columns (re-exported below for
# back-compat with code that still does `from clients.iaai import AUCTION_DATE_COL`).
from core.columns import AUCTION_DATE_COL  # noqa: E402,F401

OUTPUT_FIELDS = [
    "Make", "Model", "Year", "Odometer", "Fuel Type",
    "Lot Number", "Link", AUCTION_DATE_COL, "Location",
    "Primary Damage", "ACV",
]

# IAAI's default page size and the cap we request. PageSize=100 mirrors
# what the SPA itself uses. We only ever fetch the first page — if a
# filter row genuinely has >100 results, we log a warning (the scraper
# is not a market scraper, individual filters should be narrow).
PAGE_SIZE = 100

# Map the values users put in filters/iaai_filters.csv to the canonical
# Value IAAI's FuelTypeDesc facet expects. Probed live: 'Gasoline',
# 'Hybrid', 'Diesel', 'Electric', 'Flexible Fuel', 'Other'. CSV often
# uses 'Gas' or 'Hybrid Engine' (the existing scraper relied on a
# substring match against checkbox labels).
_FUEL_TYPE_CANONICAL = {
    "gas":            "Gasoline",
    "gasoline":       "Gasoline",
    "hybrid":         "Hybrid",
    "hybrid engine":  "Hybrid",
    "electric":       "Electric",
    "diesel":         "Diesel",
    "flexible":       "Flexible Fuel",
    "flexible fuel":  "Flexible Fuel",
    "other":          "Other",
}


# ---------------------------------------------------------------------------
# Pure helpers (no I/O)
# ---------------------------------------------------------------------------

# Filter parsing + equipment post-filter live in clients.iaai_filters so this
# module isn't 800+ lines of mixed concerns. Re-exported here so existing
# `from clients.iaai import parse_filter_row` etc. keep working.
from clients.iaai_filters import (   # noqa: E402
    _apply_segment,
    _reassemble_segments,
    apply_equipment_postfilter,
    equipment_matches,
    parse_filter_row,
    read_filters_csv,
)


def write_output_csv(path: str, records: list[dict]) -> None:
    from core.csv_io import save_csv_dict
    save_csv_dict(Path(path), OUTPUT_FIELDS, records)
    print(f"[+] Saved {len(records)} record(s) -> {path}")


def _parse_scraped_row(r: dict) -> dict | None:
    """Normalise one parsed-row dict into the canonical OUTPUT_FIELDS shape,
    converting the local-time auction date to UTC. Drops rows with no Link
    (a row template without real data is what IAAI renders for empty-result
    queries — see _ROW_SELECTOR docstring)."""
    if not isinstance(r, dict):
        return None
    record = dict.fromkeys(OUTPUT_FIELDS, "")
    record.update({k: v for k, v in r.items() if k in OUTPUT_FIELDS})
    for field in ("Year", "Make", "Model"):
        if not record[field]:
            record[field] = r.get(field, "")
    if record.get(AUCTION_DATE_COL):
        record[AUCTION_DATE_COL] = normalize_auction_date(record[AUCTION_DATE_COL])
    record["_full_title"] = r.get("_full_title", "")
    return record if record.get("Link") else None


# ---------------------------------------------------------------------------
# Payload builder (pure — no HTTP)
# ---------------------------------------------------------------------------

def build_search_payload(filters: dict,
                         page_size: int = PAGE_SIZE,
                         current_page: int = 1) -> dict:
    """Build the JSON body POSTed to IAAI's /Search endpoint for ONE filter row.

    Maps a CSV-derived filter dict (keys: make, models, year_min, year_max,
    odometer_max, fuel_type) onto the Facets / LongRanges shape the IAAI
    API accepts. Equipment is intentionally NOT mapped — there's no
    canonical IAAI facet for free-text equipment, so the existing
    post-filter (`apply_equipment_postfilter`) runs in Python after the
    response comes back.

    Always applies the daily-run baseline: AuctionDate=AuctionToday,
    StartsDesc=Run & Drive, ODOValue range 0..odometer_max (default 30000).
    Year ranges expand to one Facet per year (the API rejects a single
    `Year=YYYY-YYYY` value — empirically validated).
    """
    odo_max = filters.get("odometer_max") or 30000
    searches: list[dict] = [
        # Default=True is required — without it IAAI returns an empty page.
        {"Facets": [{"Group": "Default", "Value": "True", "ForAnalytics": False}],
         "FullSearch": None, "LongRanges": None},
        {"Facets": [{"Group": "AuctionDate", "Value": "AuctionToday"}],
         "FullSearch": None, "LongRanges": None},
        {"Facets": None, "FullSearch": None,
         "LongRanges": [{"From": 0, "Name": "ODOValue", "To": int(odo_max)}]},
        {"Facets": [{"Group": "StartsDesc", "Value": "Run & Drive"}],
         "FullSearch": None, "LongRanges": None},
    ]

    make = (filters.get("make") or "").strip().upper()
    if make:
        searches.append(
            {"Facets": [{"Group": "Make", "Value": make}],
             "FullSearch": None, "LongRanges": None}
        )

    # Model is intentionally NOT sent as a Facet. IAAI's Model facet
    # values are leaf sub-trims (e.g. "CR-V HYBRID SPORT", "CR-V HYBRID
    # AWD SPORT TOURING"); a user's CSV "CR-V HYBRID" matches a UI parent
    # checkbox that selects all sub-trims, but the API doesn't expand
    # parents. So we fetch every lot for the make+year+fuel slice and
    # post-filter on the title in Python (see `apply_model_postfilter`).

    # IAAI's Year facet only takes single-year values; a range needs one
    # Facet per year. Open-ended ranges expand to a sensible bound on the
    # missing side (current_year + 1 / 1990) — same behavior as the legacy
    # browser-clicked UI path.
    from datetime import date as _date
    year_min = filters.get("year_min")
    year_max = filters.get("year_max")
    if year_min or year_max:
        lo = int(year_min) if year_min else 1990
        hi = int(year_max) if year_max else _date.today().year + 1
        if lo <= hi:
            years = [str(y) for y in range(lo, hi + 1)]
            searches.append(
                {"Facets": [{"Group": "Year", "Value": y} for y in years],
                 "FullSearch": None, "LongRanges": None}
            )

    fuel = (filters.get("fuel_type") or "").strip()
    if fuel:
        canonical = _FUEL_TYPE_CANONICAL.get(fuel.lower(), fuel)
        searches.append(
            {"Facets": [{"Group": "FuelTypeDesc", "Value": canonical}],
             "FullSearch": None, "LongRanges": None}
        )

    # Note: SaleStatusFilters / BidStatusFilters were in the captured
    # payload from a logged-in browser session, but they came from
    # session-specific UI state. Probing showed including them with the
    # values from the capture narrows the result set in ways the legacy
    # browser scraper didn't experience (it just used default UI state).
    # Omitting them — same shape the SPA sends on a fresh session — keeps
    # the result set roughly equivalent to what the browser shows.
    return {
        "Searches":            searches,
        "ZipCode":             "",
        "miles":               0,
        "PageSize":            page_size,
        "CurrentPage":         current_page,
        "Sort":                [{"IsGeoSort": False,
                                 "SortField": "TenantSortOrder",
                                 "IsDescending": False}],
        "ShowRecommendations": False,
        "SaleStatusFilters":   [],
        "BidStatusFilters":    [],
    }


# ---------------------------------------------------------------------------
# Model post-filter (Python — IAAI's Model facet is sub-trim leaves)
# ---------------------------------------------------------------------------

def apply_model_postfilter(rows: list[dict], models: list) -> list[dict]:
    """Keep rows whose `_full_title` contains every token of any of the
    listed model names.

    Empty `models` is a no-op (returns input unchanged).

    Tokens are checked case-insensitively, ignoring extra whitespace.
    Matching "any of" means a CSV row like `Model: GLE 350; GLB 250`
    keeps a vehicle whose title contains BOTH 'GLE' AND '350', OR BOTH
    'GLB' AND '250'. This matches what the user expects from the IAAI
    UI's parent-checkbox behavior."""
    if not models:
        return list(rows)
    normalised: list[list[str]] = []
    for m in models:
        words = [w.upper() for w in str(m).split() if w.strip()]
        if words:
            normalised.append(words)
    if not normalised:
        return list(rows)
    out: list[dict] = []
    for r in rows:
        title = (r.get("_full_title") or "").upper()
        title_tokens = set(title.split())
        if any(all(w in title_tokens for w in m) for m in normalised):
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Response parser (pure — no HTTP)
# ---------------------------------------------------------------------------

_ROW_SELECTOR = ".table-row.table-row-border"


def parse_search_html(html: str) -> list[dict]:
    """Extract vehicle rows from the /Search response HTML.

    IAAI server-renders the result grid as HTML; each vehicle is one
    `.table-row.table-row-border` element with field values exposed via
    `title="..."` attributes. We translate those into the canonical
    OUTPUT_FIELDS dict via `_parse_scraped_row`.

    Empty-result queries (zero matching vehicles) render a single
    placeholder row WITHOUT a heading-link href — `_parse_scraped_row`
    drops any row missing a Link, so the empty case naturally returns [].
    """
    if not _BS4_OK:
        raise RuntimeError("beautifulsoup4 is required.  pip install beautifulsoup4 lxml")
    soup = BeautifulSoup(html, "lxml")
    raw_rows: list[dict] = []
    for r in soup.select(_ROW_SELECTOR):
        rec: dict = {}

        heading = r.select_one(".table-cell--heading a")
        if heading:
            rec["_full_title"] = heading.get_text(strip=True)
            href = heading.get("href") or ""
            if href:
                rec["Link"] = href if href.startswith("http") else IAAI_BASE + href
            parts = (rec.get("_full_title") or "").split(" ", 2)
            if len(parts) >= 1: rec["Year"]  = parts[0]
            if len(parts) >= 2: rec["Make"]  = parts[1]
            if len(parts) >= 3: rec["Model"] = parts[2]

        for attr_prefix, key in (
            ("Stock #",        "Lot Number"),
            ("Primary Damage", "Primary Damage"),
            ("Odometer",       "Odometer"),
            ("Fuel Type",      "Fuel Type"),
            ("ACV:",           "ACV"),
        ):
            el = r.find(attrs={"title":
                lambda v, p=attr_prefix: bool(v) and v.startswith(p)})
            if el:
                rec[key] = el.get_text(strip=True)

        loc_el = r.select_one('.data-list--data a[aria-label="Branch Name"]')
        if loc_el:
            rec["Location"] = loc_el.get_text(strip=True)

        date_el = r.select_one(".data-list__value--action")
        if date_el:
            rec[AUCTION_DATE_COL] = date_el.get_text(strip=True)

        raw_rows.append(rec)

    out: list[dict] = []
    for raw in raw_rows:
        parsed = _parse_scraped_row(raw)
        if parsed is not None:
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class IAAIClient(Protocol):
    """Everything scripts need from iaai.com. The main entry is
    `scrape_many` — one call per batch of filter rows; the client handles
    the actual API hits internally."""

    def scrape_many(self, filter_rows: list[dict]) -> list[dict]:
        """Run each filter set in turn, return all matching rows (already
        passed through the equipment post-filter)."""

    def scrape_with_filters(self, filters: dict, clear_filters: bool = False) -> list[dict]:
        return self.scrape_many([filters])


# ---------------------------------------------------------------------------
# Real implementation — uses a pre-warmed requests.Session
# ---------------------------------------------------------------------------

class SessionIAAIClient:
    """Live iaai.com client. Takes a `requests.Session` carrying IAAI's
    bot-defense cookies (from `clients.iaai_session.get_or_warmup_session()`)
    and POSTs one search per filter row, parses HTML response, applies
    the equipment post-filter."""

    def __init__(self, session, request_delay: float = 1.0) -> None:
        if not _REQUESTS_OK:
            raise RuntimeError("requests is required.  pip install requests")
        self._session = session
        self.request_delay = request_delay

    def scrape_many(self, filter_rows: list[dict]) -> list[dict]:
        if not filter_rows:
            return []
        out: list[dict] = []
        for idx, filters in enumerate(filter_rows, 1):
            print(f"\n[iaai {idx}/{len(filter_rows)}] "
                  f"make={filters.get('make')!r} "
                  f"models={filters.get('models')!r} "
                  f"year={filters.get('year_min')}-{filters.get('year_max')} "
                  f"odo<={filters.get('odometer_max')} "
                  f"fuel={filters.get('fuel_type')!r} "
                  f"equipment={filters.get('equipment')!r}",
                  flush=True)
            rows = self._scrape_one(filters)
            # Model is filtered in Python (see build_search_payload for why
            # IAAI's Model facet can't be used directly).
            models = filters.get("models") or []
            before_models = len(rows)
            rows = apply_model_postfilter(rows, models)
            if models:
                print(f"    [iaai] {before_models} raw / "
                      f"{before_models - len(rows)} dropped by model filter / "
                      f"{len(rows)} kept", flush=True)
            equipment = (filters.get("equipment") or "").strip()
            kept = apply_equipment_postfilter(rows, equipment)
            print(f"    [iaai] kept {len(kept)} after equipment filter",
                  flush=True)
            out.extend(kept)
            if idx < len(filter_rows):
                time.sleep(self.request_delay)
        return out

    def scrape_with_filters(self, filters: dict, clear_filters: bool = False) -> list[dict]:
        del clear_filters
        return self.scrape_many([filters])

    def _scrape_one(self, filters: dict) -> list[dict]:
        payload = build_search_payload(filters)
        # Cache-buster — IAAI's SPA appends Date.now() so the browser/CDN
        # can't serve a stale response. The server itself doesn't validate
        # the value.
        url = f"{IAAI_SEARCH_URL}?c={int(time.time() * 1000)}"
        try:
            resp = self._session.post(url, json=payload, timeout=30,
                                      allow_redirects=False)
        except requests.RequestException as e:
            print(f"    [iaai] request error: {e!r}", flush=True)
            return []
        if resp.status_code != 200:
            print(f"    [iaai] HTTP {resp.status_code} "
                  f"(size={len(resp.content)}B); skipping this filter row",
                  flush=True)
            return []

        rows = parse_search_html(resp.text)
        print(f"    [iaai] {len(rows)} raw row(s) returned "
              f"(response body {len(resp.content) // 1024} KB)", flush=True)
        if len(rows) >= PAGE_SIZE:
            # The first page is full — there may be more. We deliberately
            # don't paginate; daily filters should be narrow enough that
            # PAGE_SIZE is plenty. Surface a warning so we notice if a
            # filter ever needs broadening / tightening.
            print(f"    [iaai] [warn] {len(rows)} rows == PAGE_SIZE — "
                  f"results may be truncated; consider tightening the filter",
                  flush=True)
        return rows


# ---------------------------------------------------------------------------
# Fake (in-memory) implementation for tests
# ---------------------------------------------------------------------------

class FakeIAAIClient:
    """In-memory IAAIClient.

    Two modes:
      - flat:      FakeIAAIClient(rows=[...])                returns same list every call
      - callable:  FakeIAAIClient(scrape_fn=lambda f: ...)   compute per-filter result
    """

    def __init__(
        self,
        rows: list[dict] | None = None,
        scrape_fn: Callable[[dict], list[dict]] | None = None,
    ) -> None:
        self._rows      = list(rows or [])
        self._scrape_fn = scrape_fn
        self.calls: list[dict] = []

    def scrape_many(self, filter_rows: list[dict]) -> list[dict]:
        out: list[dict] = []
        for filters in filter_rows:
            out.extend(self._scrape_one(filters))
        return out

    def scrape_with_filters(self, filters: dict, clear_filters: bool = False) -> list[dict]:
        del clear_filters
        return self._scrape_one(filters)

    def _scrape_one(self, filters: dict) -> list[dict]:
        self.calls.append(dict(filters))
        if self._scrape_fn is not None:
            return list(self._scrape_fn(filters))
        return list(self._rows)
