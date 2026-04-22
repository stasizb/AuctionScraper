#!/usr/bin/env python3
"""
Copart.com Web Scraper
Использует реальный внутренний API Copart (тот же, что используется на сайте).

Фильтры "Run and Drive" (CERT-D) и "Auction within 24h" применяются ВСЕГДА.
Фильтр "Equipment" применяется пост-фильтрацией по URL лота.

Использование:
    python copart_scraper.py --input filters.csv --output results.csv [--delay 2]

Формат входного CSV (одна строка = один поисковый запрос):
    Make: Mercedes-Benz, Model: Gle, Year min:2022, Year max: 2025, Odometer max: 25000, Fuel Type: Gas, Equipment: 4MATIC
    Make: Lincoln, Model: Nautilus;Corsair, Year min: 2024, Odometer max: 25000

Поддерживаемые поля:
    Make         — марка (обязательно)
    Model        — модель, несколько через ";" (обязательно)
    Year min     — год от (опционально)
    Year max     — год до (опционально, по умолчанию текущий год + 1)
    Odometer max — пробег до (опционально)
    Fuel Type    — тип топлива (опционально, по умолчанию GAS)
    Equipment    — комплектация, пост-фильтр по URL лота (опционально)
"""

import argparse
import csv
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL       = "https://www.copart.com"
SEARCH_API_URL = f"{BASE_URL}/public/lots/search-results"

# Headers that closely mimic a real Chrome browser request
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type":    "application/json",
    "Origin":          BASE_URL,
    "Referer":         f"{BASE_URL}/lotSearchResults/",
    "sec-ch-ua":       '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
}

PAGE_SIZE     = 100
REQUEST_DELAY = 2.0


# ---------------------------------------------------------------------------
# CSV parser
# Each row looks like:
#   Make: Mercedes-Benz, Model: Gle;GLE43, Year min:2022, Year max: 2025, ...
# ---------------------------------------------------------------------------

def parse_filter_row(raw_line: str) -> dict:
    """Parse a single filter line into a dict of filter parameters."""
    filters = {}
    segments = [s.strip() for s in raw_line.split(",")]
    for seg in segments:
        m = re.match(r"^([^:]+?)\s*:\s*(.+)$", seg)
        if not m:
            continue
        key = m.group(1).strip().lower().replace(" ", "_")
        val = m.group(2).strip()

        if key == "make":
            filters["make"] = val.upper()
        elif key == "model":
            # Support multiple models separated by ";"
            filters["models"] = [v.strip().upper() for v in val.split(";") if v.strip()]
        elif key in ("year_min", "yearmin"):
            filters["year_min"] = int(val)
        elif key in ("year_max", "yearmax"):
            filters["year_max"] = int(val)
        elif key in ("odometer_max", "odo_max", "odometer"):
            filters["odometer_max"] = int(val)
        elif key in ("fuel_type", "fueltype", "fuel"):
            filters["fuel_type"] = val.strip().upper()
        elif key in ("equipment", "trim"):
            filters["equipment"] = val

    return filters


def read_filters_csv(path: str) -> list[dict]:
    """Read CSV file; each non-empty, non-comment row is a filter line."""
    filters_list = []
    with open(path, newline="", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            filters_list.append(parse_filter_row(line))
    return filters_list


# ---------------------------------------------------------------------------
# Build Copart search payload
#
# Filter keys (confirmed from real Copart browser URLs):
#   FETI -> lot_condition_code:CERT-D      (Run and Drive — always on)
#   SDAT -> auction_date_utc:[ISO TO ISO]  (24h window    — always on)
#   YEAR -> lot_year:[YYYY TO YYYY]
#   MAKE -> lot_make_desc:"MAKE"
#   MODL -> lot_model_desc:"MODEL"         (array for multiple models)
#   FUEL -> fuel_type_desc:"HYBRID ENGINE" (full Copart label, uppercase)
#   ODM  -> odometer_reading_received:[0 TO N]
# ---------------------------------------------------------------------------

def build_search_payload(filters: dict, page: int = 0) -> dict:
    make   = filters.get("make", "")
    models = filters.get("models") or []

    api_filter = {}

    # ---- Always: Run and Drive ----
    api_filter["FETI"] = ["lot_condition_code:CERT-D"]

    # ---- Year range ----
    year_min = filters.get("year_min")
    year_max = filters.get("year_max") or (datetime.now(tz=timezone.utc).year + 1)
    y_from   = year_min if year_min else "*"
    api_filter["YEAR"] = [f"lot_year:[{y_from} TO {year_max}]"]

    # ---- Make ----
    if make:
        api_filter["MAKE"] = [f'lot_make_desc:"{make}"']

    # ---- Model (one or many) ----
    if models:
        api_filter["MODL"] = [f'lot_model_desc:"{m}"' for m in models]

    # ---- Fuel type (only if explicitly specified, no default) ----
    fuel = filters.get("fuel_type")
    if fuel:
        api_filter["FUEL"] = [f'fuel_type_desc:"{fuel}"']

    # ---- Odometer ----
    odometer_max = filters.get("odometer_max")
    if odometer_max is not None:
        api_filter["ODM"] = [f"odometer_reading_received:[0 TO {odometer_max}]"]

    # ---- Always: Auction today or tomorrow (48h window covers both days; matches Copart order) ----
    now         = datetime.now(tz=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_window  = today_start + timedelta(hours=47, minutes=59, seconds=59)
    date_from   = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_to     = end_window.strftime("%Y-%m-%dT%H:%M:%SZ")
    api_filter["SDAT"] = [f'auction_date_utc:["{date_from}" TO "{date_to}"]']

    return {
        "query":          ["*"],
        "filter":         api_filter,
        "sort":           None,
        "page":           page,
        "size":           PAGE_SIZE,
        "start":          page * PAGE_SIZE,
        "watchListOnly":  False,
        "freeFormSearch": False,
        "searchName":     "",
    }


# ---------------------------------------------------------------------------
# HTTP session — mimic a real browser as closely as possible
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    # Step 1: visit homepage to get initial cookies (JSESSIONID etc.)
    try:
        r = session.get(BASE_URL, timeout=15)
        log.info(f"Homepage: HTTP {r.status_code} | cookies: {list(session.cookies.keys())}")
    except Exception as e:
        log.warning(f"Homepage visit failed: {e}")

    # Step 2: visit the search results page to get any additional cookies
    try:
        r = session.get(f"{BASE_URL}/lotSearchResults/", timeout=15)
        log.info(f"Search page: HTTP {r.status_code} | cookies: {list(session.cookies.keys())}")
    except Exception as e:
        log.warning(f"Search page visit failed: {e}")

    return session


# ---------------------------------------------------------------------------
# Fetch lots from API (all pages)
# ---------------------------------------------------------------------------

def fetch_lots(session: requests.Session, filters: dict) -> list[dict]:
    all_lots = []
    page = 0

    while True:
        payload = build_search_payload(filters, page=page)
        log.info(
            f"  Page {page} | query={payload['query']} "
            f"| filter keys={list(payload['filter'].keys())}"
        )
        log.info(f"  Payload: {json.dumps(payload)}")

        try:
            resp = session.post(SEARCH_API_URL, json=payload, timeout=30)
            log.info(f"  Response: HTTP {resp.status_code} | size={len(resp.content)} bytes")
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as e:
            log.error(f"  HTTP {e.response.status_code}: {e.response.text[:600]}")
            break
        except Exception as e:
            log.error(f"  Request error: {e}")
            break

        # Navigate response — Copart wraps results under data.results.content
        results_data   = (data.get("data") or {}).get("results") or {}
        content        = results_data.get("content", [])
        total_elements = results_data.get("totalElements", 0)

        if not content:
            log.info("  Empty content, stopping.")
            break

        all_lots.extend(content)
        log.info(f"  Got {len(content)} lots (total: {total_elements})")

        if len(all_lots) >= total_elements or len(content) < PAGE_SIZE:
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    return all_lots


# ---------------------------------------------------------------------------
# Post-filter: Equipment (check lot URL slug / title)
# ---------------------------------------------------------------------------

def build_lot_url(lot: dict) -> str:
    lot_number = (
        lot.get("lotNumberStr")
        or lot.get("ln")
        or str(lot.get("lotNumber", ""))
        or ""
    )
    description = (
        lot.get("ld")
        or lot.get("lotName")
        or lot.get("description")
        or ""
    ).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", description).strip("-")
    if slug:
        return f"{BASE_URL}/lot/{lot_number}/{slug}"
    return f"{BASE_URL}/lot/{lot_number}"


def equipment_ok(lot: dict, equipment: str | None) -> bool:
    if not equipment:
        return True
    eq    = re.sub(r"[\s\-]+", "", equipment.lower())
    url   = re.sub(r"[\-]+", "", build_lot_url(lot).lower())
    title = re.sub(r"[\s\-]+", "", (
        lot.get("ld") or lot.get("lotName") or lot.get("description") or ""
    ).lower())
    return eq in url or eq in title


# ---------------------------------------------------------------------------
# Convert lot dict → output CSV row
# ---------------------------------------------------------------------------

def lot_to_row(lot: dict, filters: dict) -> dict:
    lot_number = (
        lot.get("lotNumberStr")
        or lot.get("ln")
        or str(lot.get("lotNumber", ""))
        or ""
    )
    sale_ts = lot.get("ad") or lot.get("auctionDateUTC")
    auction_date_str = ""
    if sale_ts:
        try:
            dt = datetime.fromtimestamp(int(sale_ts) / 1000, tz=timezone.utc)
            auction_date_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            auction_date_str = str(sale_ts)

    return {
        "Make":           lot.get("mkn") or lot.get("make") or filters.get("make", ""),
        "Model":          lot.get("lm")  or lot.get("model") or ";".join(filters.get("models") or []),
        "Year":           lot.get("lcy") or lot.get("lotYear") or "",
        "Odometer":       lot.get("orr") or lot.get("od") or "",
        "Fuel Type":      lot.get("ftd") or lot.get("fuelType") or "",
        "Lot Number":     lot_number,
        "Link":           build_lot_url(lot),
        "Auction Date":   auction_date_str,
        "Location":       lot.get("yn")  or lot.get("yard") or "",
        "Primary Damage": lot.get("dd")  or "",
    }


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def process_filters(filters: dict, session: requests.Session) -> list[dict]:
    equipment = filters.get("equipment")
    log.info(
        f"  make={filters.get('make')} models={filters.get('models')} "
        f"year={filters.get('year_min')}-{filters.get('year_max')} "
        f"odo<={filters.get('odometer_max')} fuel={filters.get('fuel_type')} "
        f"equipment={equipment}"
    )

    raw_lots = fetch_lots(session, filters)
    log.info(f"  {len(raw_lots)} lots from API → applying equipment post-filter...")

    matched = []
    for lot in raw_lots:
        if not equipment_ok(lot, equipment):
            continue
        matched.append(lot_to_row(lot, filters))

    log.info(f"  {len(matched)} lots passed equipment filter.")
    return matched


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    from datetime import date
    today = date.today().strftime("%Y_%m_%d")

    parser = argparse.ArgumentParser(description="Copart lot scraper")
    parser.add_argument("--input",  "-i", default="copart_filters.csv",
                        help="Input CSV with filter rows (default: copart_filters.csv)")
    parser.add_argument("--output", "-o", default=f"copart_search_{today}.csv",
                        help=f"Output CSV file (default: copart_search_{today}.csv)")
    parser.add_argument("--delay",  "-d", type=float, default=2.0, help="Delay between requests (s)")
    args = parser.parse_args()

    global REQUEST_DELAY
    REQUEST_DELAY = args.delay

    input_path = Path(args.input)
    if not input_path.exists():
        log.error(f"Input file not found: {input_path}")
        return

    filters_list = read_filters_csv(str(input_path))
    log.info(f"Loaded {len(filters_list)} filter row(s) from {input_path}")
    if not filters_list:
        log.error("No filter rows found. Check your CSV format.")
        return

    session  = make_session()
    all_rows = []

    for idx, filters in enumerate(filters_list, 1):
        log.info(f"[{idx}/{len(filters_list)}] Processing filter row...")
        try:
            rows = process_filters(filters, session)
            all_rows.extend(rows)
        except Exception as e:
            log.error(f"  Error: {e}")
        time.sleep(REQUEST_DELAY)

    output_path = Path(args.output)
    fieldnames  = [
        "Make", "Model", "Year", "Odometer", "Fuel Type",
        "Lot Number", "Link", "Auction Date", "Location", "Primary Damage",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    if all_rows:
        log.info(f"✓ Saved {len(all_rows)} result(s) to {output_path}")
    else:
        log.warning(f"No matching lots found. Empty file written to {output_path}")


if __name__ == "__main__":
    main()