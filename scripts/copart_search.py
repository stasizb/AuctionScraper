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
    Age          — возраст в годах: Year min = текущий год − Age, Year max не задаётся.
                   Age имеет приоритет над Year min / Year max (опционально)
    Odometer max — пробег до (опционально)
    Fuel Type    — тип топлива (опционально, по умолчанию GAS)
    Equipment    — комплектация, пост-фильтр по URL лота (опционально)
"""

import argparse
import csv
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients import copart as copart_client
from core.filters import apply_age_filter

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

REQUEST_DELAY = 2.0
BASE_URL      = copart_client.BASE_URL


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
        elif key == "age":
            try:
                filters["age"] = int(val)
            except ValueError:
                pass
        elif key in ("odometer_max", "odo_max", "odometer"):
            filters["odometer_max"] = int(val)
        elif key in ("fuel_type", "fueltype", "fuel"):
            filters["fuel_type"] = val.strip().upper()
        elif key in ("equipment", "trim"):
            filters["equipment"] = val

    return apply_age_filter(filters)


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
    # Multi-word equipment ("Premium 45") matches when every word appears
    # in the haystack, in any order — Copart sometimes interleaves trim
    # tokens ("Premium Plus 45") so a single concatenated substring check
    # would miss valid lots.
    words = [re.sub(r"[\s\-]+", "", w.lower()) for w in equipment.split() if w.strip()]
    if not words:
        return True
    # The trim ("4MATIC", "Touring", "Premium 45", ...) can live in any of
    # several Copart API fields depending on the lot — `ld`, `lm`, `lcd`,
    # `tlt`, etc. — and Copart's frontend reassembles the displayed title
    # from multiple of them. So scan every string-valued field in the lot
    # plus the constructed URL.
    haystack_parts = [build_lot_url(lot)]
    for v in lot.values():
        if isinstance(v, str):
            haystack_parts.append(v)
    haystack = re.sub(r"[\s\-]+", "", " ".join(haystack_parts).lower())
    return all(w in haystack for w in words)


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
        except (ValueError, TypeError, OSError, OverflowError):
            # Copart sometimes returns nonsensical timestamps (string,
            # negative, far-future). Keep the raw value so a human can
            # inspect rather than silently dropping the row.
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

def process_filters(filters: dict, client: copart_client.CopartClient) -> list[dict]:
    equipment = filters.get("equipment")
    log.info(
        f"  make={filters.get('make')} models={filters.get('models')} "
        f"year={filters.get('year_min')}-{filters.get('year_max')} "
        f"odo<={filters.get('odometer_max')} fuel={filters.get('fuel_type')} "
        f"equipment={equipment}"
    )

    raw_lots = client.fetch_lots(filters)
    log.info(f"  {len(raw_lots)} lots from API → applying equipment post-filter...")

    matched = []
    for lot in raw_lots:
        if not equipment_ok(lot, equipment):
            if equipment:
                title = lot.get("ld") or lot.get("lotName") or lot.get("description") or ""
                lot_num = lot.get("lotNumberStr") or lot.get("ln") or lot.get("lotNumber") or "?"
                log.info(f"    drop {lot_num}: {title!r} — no {equipment!r} in title/URL")
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

    client   = copart_client.HttpCopartClient(request_delay=args.delay)
    all_rows = []

    for idx, filters in enumerate(filters_list, 1):
        log.info(f"[{idx}/{len(filters_list)}] Processing filter row...")
        try:
            rows = process_filters(filters, client)
            all_rows.extend(rows)
        except Exception as e:
            log.error(f"  Error: {e}")
        time.sleep(REQUEST_DELAY)

    from core.csv_io import save_csv_dict
    output_path = Path(args.output)
    fieldnames  = [
        "Make", "Model", "Year", "Odometer", "Fuel Type",
        "Lot Number", "Link", "Auction Date", "Location", "Primary Damage",
    ]
    save_csv_dict(output_path, fieldnames, all_rows)

    if all_rows:
        log.info(f"✓ Saved {len(all_rows)} result(s) to {output_path}")
    else:
        log.warning(f"No matching lots found. Empty file written to {output_path}")


if __name__ == "__main__":
    main()