#!/usr/bin/env python3
"""
bidfax.info Price Scraper
=========================
Reads a search CSV (produced by iaai_search.py or copart_search.py),
looks up each Lot Number on bidfax.info, and writes a new CSV with
three extra columns: Price, VIN, and an updated Link.

  Price  — "$X,XXX" if a final sale price is found, "In Progress" otherwise
  VIN    — extracted from the bidfax result URL (empty if not found)
  Link   — bidfax.info URL if found, otherwise the original auction link

bidfax.info aggregates both Copart and IAAI — no auction selector needed.

Only final prices are cached. "In Progress" rows are retried on the next run.

USAGE:
  python bidfax_search.py
  python bidfax_search.py --auction iaai
  python bidfax_search.py --auction copart --output my_output.csv
  python bidfax_search.py --delay 3 --cache bidfax_cache.json
"""

import argparse
import asyncio
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import bidfax_lib

try:
    import nodriver as uc
    _NODRIVER_OK = True
except ImportError:
    _NODRIVER_OK = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRICE_COL             = "Price"
VIN_COL               = "VIN"
DELETION_LOG          = "bidfax_deletions.json"
SALE_ENDED_TEXT       = "Sale ended"
COPART_RENDER_WAIT    = 4   # seconds to wait for Angular to render lot page
_MAX_CONCURRENT_CHECKS = 5  # parallel Copart lot-page checks


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def _build_output_fieldnames(src_fieldnames: list[str]) -> list[str]:
    """Insert Price after Odometer, append VIN at end."""
    cols = list(src_fieldnames)
    insert_at = cols.index("Odometer") + 1 if "Odometer" in cols else len(cols)
    cols.insert(insert_at, PRICE_COL)
    cols.append(VIN_COL)
    return cols


def _build_output_row(src_row: dict, price: str, vin: str, bidfax_url: str) -> dict:
    """Return a copy of src_row with Price, VIN set and Link updated if found."""
    row = dict(src_row)
    row[PRICE_COL] = price
    row[VIN_COL]   = vin
    if bidfax_url:
        row["Link"] = bidfax_url
    return row


# ---------------------------------------------------------------------------
# Deletion log
# ---------------------------------------------------------------------------

def _append_deletion_log(log_path: Path, input_file: str, deleted_rows: list[dict]) -> None:
    existing = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
        except ValueError:
            existing = []
    entry = {
        "run_date":      datetime.now().isoformat(),
        "input_file":    input_file,
        "deleted_items": deleted_rows,
    }
    existing.append(entry)
    log_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _remove_from_input(input_path: Path, deleted_lots: set[str]) -> None:
    with input_path.open(newline="", encoding="utf-8-sig") as fh:
        reader     = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows       = list(reader)

    kept    = [r for r in rows if str(r.get("Lot Number", "")).strip() not in deleted_lots]
    removed = len(rows) - len(kept)

    with input_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)

    print(f"[*] Removed {removed} lot(s) from input file: {input_path.name}")


# ---------------------------------------------------------------------------
# Copart + bidfax combined async runner
# ---------------------------------------------------------------------------

async def _bidfax_lookup(page, lot: str, make: str) -> tuple[str, str, str]:
    """Search bidfax for *lot*, retrying up to 3 times if the returned URL make
    does not match *make*. Returns (_IN_PROGRESS, "", "") when no valid match found."""
    for attempt in range(3):
        await page.get(bidfax_lib.BIDFAX_HOME)
        await asyncio.sleep(2)
        await bidfax_lib._wait_cf_clear(page)
        price, vin, url = await bidfax_lib.search_bidfax(page, lot)
        if not url or bidfax_lib.url_make_matches(make, url):
            return price, vin, url
        print(f"(retry {attempt + 1}, make mismatch) ", end="", flush=True)
    return bidfax_lib._IN_PROGRESS, "", ""


async def _check_sale_ended(browser, row: dict, sem: asyncio.Semaphore) -> tuple[dict, bool]:
    """Open a new tab to check one Copart lot page. Returns (row, sale_ended)."""
    async with sem:
        link = str(row.get("Link", "")).strip()
        page = await browser.get(link, new_tab=True)
        await asyncio.sleep(COPART_RENDER_WAIT)
        ended = SALE_ENDED_TEXT in await page.get_content()
        await page.close()
        return row, ended


async def _sale_ended_filter(
    browser, rows: list[dict], cache: dict
) -> tuple[dict[str, tuple], list[dict], list[dict]]:
    """Check each Copart row in parallel. Returns (cached_results, to_lookup, deleted_rows)."""
    cached:    dict[str, tuple] = {}
    unchecked: list[dict]       = []
    for row in rows:
        lot = str(row.get("Lot Number", "")).strip()
        if lot in cache:
            cached[lot] = cache[lot]
        else:
            unchecked.append(row)

    to_lookup:    list[dict] = []
    deleted_rows: list[dict] = []

    if unchecked:
        sem     = asyncio.Semaphore(_MAX_CONCURRENT_CHECKS)
        results = await asyncio.gather(
            *(_check_sale_ended(browser, r, sem) for r in unchecked)
        )
        for row, ended in results:
            lot = str(row.get("Lot Number", "")).strip()
            if ended:
                print(f"  [copart] Lot {lot} — Sale ended → queuing bidfax lookup")
                to_lookup.append(row)
            else:
                print(f"  [copart] Lot {lot} — NOT ended → removing")
                deleted_rows.append(row)

    return cached, to_lookup, deleted_rows


async def _bidfax_lookups(
    page, to_lookup: list[dict], delay: float
) -> dict[str, tuple]:
    """Run bidfax searches for each row. Returns {lot: (price, vin, url)}."""
    results: dict[str, tuple] = {}
    print(f"\n[*] Opening bidfax.info for {len(to_lookup)} lot(s)…")
    await page.get(bidfax_lib.BIDFAX_HOME)
    print("[*] Waiting for bidfax.info (Cloudflare)…")
    await bidfax_lib._wait_cf_clear(page)
    print("[+] Ready.")
    for i, row in enumerate(to_lookup, 1):
        lot  = str(row.get("Lot Number", "")).strip()
        make = str(row.get("Make", "")).strip()
        print(f"  [bidfax {i}/{len(to_lookup)}] {lot!r} … ", end="", flush=True)
        price, vin, url = await _bidfax_lookup(page, lot, make)
        results[lot] = (price, vin, url)
        print(f"{price}  VIN:{vin or '—'}  {url or 'not found'}")
        if i < len(to_lookup):
            await asyncio.sleep(delay)
    return results


async def _run_copart_async(
    rows: list[dict],
    delay: float,
    cache: dict,
    browser_port: int | None = None,
) -> tuple[dict[str, tuple], list[dict]]:
    """Run copart sale-ended filter then bidfax lookups in one browser session."""
    if browser_port:
        browser = await uc.start(host="127.0.0.1", port=browser_port)
    else:
        browser = await uc.start(
            headless=False,
            sandbox=False,
            browser_args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    try:
        page = await browser.get("about:blank")
        cached, to_lookup, deleted_rows = await _sale_ended_filter(browser, rows, cache)
        bidfax_results = await _bidfax_lookups(page, to_lookup, delay) if to_lookup else {}
    finally:
        try:
            await browser.stop()
        except Exception:
            pass

    return {**cached, **bidfax_results}, deleted_rows


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process(
    input_path: Path,
    output_path: Path,
    cache_path: Path,
    delay: float,
    auction: str,
    log_path: Path,
    browser_port: int | None = None,
) -> None:
    with input_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            sys.exit("Input CSV is empty.")
        if "Lot Number" not in reader.fieldnames:
            sys.exit(f"'Lot Number' column not found. Available: {list(reader.fieldnames)}")
        rows      = list(reader)
        src_names = list(reader.fieldnames)

    out_names = _build_output_fieldnames(src_names)

    # Active rows only (skip blank / comment)
    active_rows = [
        r for r in rows
        if str(r.get("Make", "")).strip()
        and not str(r.get("Make", "")).strip().startswith("#")
        and str(r.get("Lot Number", "")).strip()
    ]
    lots = [str(r.get("Lot Number", "")).strip() for r in active_rows]

    print(f"[*] {len(lots)} lot(s) to process")

    deleted_rows: list[dict] = []

    if auction == "copart":
        if not _NODRIVER_OK:
            sys.exit("[!] nodriver is required for copart auction. pip install nodriver")
        cache = bidfax_lib.load_cache(cache_path)
        results, deleted_rows = asyncio.run(_run_copart_async(active_rows, delay, cache, browser_port=browser_port))
        # Persist only final prices
        cache.update({lot: v for lot, v in results.items() if v[0] != "In Progress"})
        bidfax_lib.save_cache(cache_path, cache)
    else:
        makes   = {str(r.get("Lot Number", "")).strip(): str(r.get("Make", "")).strip()
                   for r in active_rows}
        results = bidfax_lib.run_batch(lots, delay, cache_path, makes=makes, browser_port=browser_port)

    # Handle deletions (copart only)
    if deleted_rows:
        deleted_lots = {str(r.get("Lot Number", "")).strip() for r in deleted_rows}
        _append_deletion_log(log_path, input_path.name, deleted_rows)
        print(f"[*] Logged {len(deleted_rows)} deletion(s) to: {log_path.name}")
        _remove_from_input(input_path, deleted_lots)

    deleted_lots_set = {str(r.get("Lot Number", "")).strip() for r in deleted_rows}

    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=out_names, extrasaction="ignore")
        writer.writeheader()
        for row in active_rows:
            lot = str(row.get("Lot Number", "")).strip()
            if lot in deleted_lots_set:
                continue
            price, vin, url = results.get(lot, ("In Progress", "", ""))
            writer.writerow(_build_output_row(row, price, vin, url))

    print(f"[+] Saved → {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _find_recent_search(directory: Path, auction: str, date_str: str, max_days: int = 7) -> str | None:
    """Return the date string (YYYY_MM_DD) of the closest existing search CSV
    at or before date_str, scanning back up to max_days. Returns None if not found.
    """
    try:
        d = date.fromisoformat(date_str.replace("_", "-"))
    except ValueError:
        return None
    for offset in range(max_days):
        candidate = d - timedelta(days=offset)
        path = directory / f"{auction}_search_{candidate.strftime('%Y_%m_%d')}.csv"
        if path.exists():
            return candidate.strftime("%Y_%m_%d")
    return None


def main() -> None:
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y_%m_%d")

    parser = argparse.ArgumentParser(
        description="Look up auction lots on bidfax.info and record prices.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--auction", "-a", default="copart",
                        help="Auction name: copart or iaai (default: copart)")
    parser.add_argument("--date",    "-t", default=None,
                        help="Date for input file in yyyy_mm_dd format (default: yesterday)")
    parser.add_argument("--dir",     "-D", default=".",
                        help="Directory for input/output CSV files (default: current dir)")
    parser.add_argument("--output",  "-o", default=None,
                        help="Output CSV path (default: <dir>/<auction>_price_YYYY_MM_DD.csv)")
    parser.add_argument("--cache",   "-c", default="bidfax_cache.json",
                        help="Cache file for bidfax lookups (default: bidfax_cache.json)")
    parser.add_argument("--log",     "-l", default=DELETION_LOG,
                        help=f"Deletion log file (default: {DELETION_LOG})")
    parser.add_argument("--delay",        "-d", type=float, default=2.0,
                        help="Seconds between searches (default: 2.0)")
    parser.add_argument("--browser-port", type=int, default=None,
                        help="Connect to a running Chrome on this port instead of launching one")
    args = parser.parse_args()

    file_dir  = Path(args.dir).resolve()
    file_date = args.date or yesterday
    input_path = file_dir / f"{args.auction}_search_{file_date}.csv"

    if not input_path.exists():
        resolved = _find_recent_search(file_dir, args.auction, file_date)
        if resolved:
            file_date  = resolved
            input_path = file_dir / f"{args.auction}_search_{file_date}.csv"
            print(f"[*] Requested date not found — using: {input_path.name}")
        else:
            sys.exit(f"Input file not found: {input_path}")

    output_path = Path(args.output) if args.output else file_dir / f"{args.auction}_price_{file_date}.csv"
    cache_path  = Path(args.cache)
    log_path    = Path(args.log)

    print(f"Auction  : {args.auction}")
    print(f"Input    : {input_path}")
    print(f"Output   : {output_path}")
    print(f"Cache    : {cache_path}")
    print(f"Log      : {log_path}")
    print(f"Delay    : {args.delay}s\n")

    process(input_path, output_path, cache_path, args.delay, args.auction, log_path,
            browser_port=args.browser_port)


if __name__ == "__main__":
    main()
