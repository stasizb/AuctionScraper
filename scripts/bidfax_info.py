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
  python scripts/bidfax_info.py
  python scripts/bidfax_info.py --auction iaai
  python scripts/bidfax_info.py --auction copart --output my_output.csv
  python scripts/bidfax_info.py --delay 3 --cache bidfax_cache.json
"""

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients import bidfax
from core.columns import LOT_COL, MAKE_COL, PRICE_COL, VIN_COL

DELETION_LOG = "bidfax_deletions.json"


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _build_output_fieldnames(src_fieldnames: list[str]) -> list[str]:
    cols = list(src_fieldnames)
    insert_at = cols.index("Odometer") + 1 if "Odometer" in cols else len(cols)
    cols.insert(insert_at, PRICE_COL)
    cols.append(VIN_COL)
    return cols


def _build_output_row(src_row: dict, price: str, vin: str, bidfax_url: str) -> dict:
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
    existing.append({
        "run_date":      datetime.now().isoformat(),
        "input_file":    input_file,
        "deleted_items": deleted_rows,
    })
    log_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _remove_from_input(input_path: Path, deleted_lots: set[str]) -> None:
    with input_path.open(newline="", encoding="utf-8-sig") as fh:
        reader     = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows       = list(reader)
    kept    = [r for r in rows if str(r.get(LOT_COL, "")).strip() not in deleted_lots]
    removed = len(rows) - len(kept)
    with input_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)
    print(f"[*] Removed {removed} lot(s) from input file: {input_path.name}")


# ---------------------------------------------------------------------------
# Copart flow — sale-ended filter + bidfax lookup via the client
# ---------------------------------------------------------------------------

def _run_copart(
    active_rows: list[dict],
    delay: float,
    cache_path: Path,
    client: bidfax.BidfaxClient,
    max_concurrent: int = 1,
) -> tuple[dict[str, tuple], list[dict]]:
    """Returns (results_by_lot, deleted_rows)."""
    cache = bidfax.load_cache(cache_path)

    cached_results: dict[str, tuple] = {}
    to_check: list[dict] = []
    for row in active_rows:
        lot = str(row.get(LOT_COL, "")).strip()
        if lot in cache:
            cached_results[lot] = cache[lot]
        else:
            to_check.append(row)

    deleted_rows: list[dict] = []
    if not to_check:
        return cached_results, deleted_rows

    url_to_row = {str(r.get("Link", "")).strip(): r for r in to_check if str(r.get("Link", "")).strip()}
    ended_map  = client.check_sale_ended_many(list(url_to_row.keys()))

    to_lookup: list[dict] = []
    for url, ended in ended_map.items():
        row = url_to_row[url]
        lot = str(row.get(LOT_COL, "")).strip()
        if ended:
            print(f"  [copart] Lot {lot} — Sale ended → queuing bidfax lookup")
            to_lookup.append(row)
        else:
            print(f"  [copart] Lot {lot} — NOT ended → removing")
            deleted_rows.append(row)

    fetched: dict[str, tuple] = {}
    if to_lookup:
        queries = [str(r.get(LOT_COL, "")).strip() for r in to_lookup]
        makes   = {str(r.get(LOT_COL, "")).strip(): str(r.get(MAKE_COL, "")).strip()
                   for r in to_lookup}
        fetched = client.lookup_many(queries, makes=makes, delay=delay,
                                     max_concurrent=max_concurrent)
        cache.update({lot: v for lot, v in fetched.items() if v[0] != bidfax.IN_PROGRESS})
        bidfax.save_cache(cache_path, cache)

    return {**cached_results, **fetched}, deleted_rows


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
    client: bidfax.BidfaxClient | None = None,
    max_concurrent: int = 1,
) -> None:
    with input_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            sys.exit("Input CSV is empty.")
        if LOT_COL not in reader.fieldnames:
            sys.exit(f"'Lot Number' column not found. Available: {list(reader.fieldnames)}")
        rows      = list(reader)
        src_names = list(reader.fieldnames)

    out_names = _build_output_fieldnames(src_names)

    active_rows = [
        r for r in rows
        if str(r.get(MAKE_COL, "")).strip()
        and not str(r.get(MAKE_COL, "")).strip().startswith("#")
        and str(r.get(LOT_COL, "")).strip()
    ]
    lots = [str(r.get(LOT_COL, "")).strip() for r in active_rows]
    print(f"[*] {len(lots)} lot(s) to process")

    deleted_rows: list[dict] = []

    if auction == "copart":
        real_client = client or bidfax.BrowserBidfaxClient(browser_port=browser_port)
        results, deleted_rows = _run_copart(
            active_rows, delay, cache_path, real_client,
            max_concurrent=max_concurrent,
        )
    else:
        makes = {str(r.get(LOT_COL, "")).strip(): str(r.get(MAKE_COL, "")).strip()
                 for r in active_rows}
        results = bidfax.run_batch(
            lots, delay, cache_path,
            makes=makes, browser_port=browser_port, client=client,
            max_concurrent=max_concurrent,
        )

    if deleted_rows:
        deleted_lots = {str(r.get(LOT_COL, "")).strip() for r in deleted_rows}
        _append_deletion_log(log_path, input_path.name, deleted_rows)
        print(f"[*] Logged {len(deleted_rows)} deletion(s) to: {log_path.name}")
        _remove_from_input(input_path, deleted_lots)

    deleted_lots_set = {str(r.get(LOT_COL, "")).strip() for r in deleted_rows}

    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=out_names, extrasaction="ignore")
        writer.writeheader()
        for row in active_rows:
            lot = str(row.get(LOT_COL, "")).strip()
            if lot in deleted_lots_set:
                continue
            price, vin, url = results.get(lot, (bidfax.IN_PROGRESS, "", ""))
            writer.writerow(_build_output_row(row, price, vin, url))

    print(f"[+] Saved → {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _find_recent_search(directory: Path, auction: str, date_str: str, max_days: int = 7) -> str | None:
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
    parser.add_argument("--concurrent", type=int, default=1,
                        help="Parallel bidfax tabs (default: 1 = sequential; experimental)")
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
            browser_port=args.browser_port, max_concurrent=args.concurrent)


if __name__ == "__main__":
    main()
