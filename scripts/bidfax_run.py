#!/usr/bin/env python3
"""
Consolidated bidfax pipeline — one browser session for all bidfax work.

Replaces the separate runs of:
  - bidfax_info.py copart  (Sale-Ended check + bidfax for newly-ended Copart lots)
  - bidfax_info.py iaai    (bidfax for today's IAAI lots)
  - price_refresh.py       (re-fetch In Progress prices in older price CSVs)

Phases (one asyncio.run, one fresh Chrome):
  1. Build a deduped bidfax queue with destination metadata per lot.
  2. Run bidfax lookup once (multi-tab, cache-aware).
  3. Distribute results: write/update CSV files and (optional) workbook.

The Chrome session is intentionally launched fresh (no --browser-port) so it
doesn't inherit cookies / Cloudflare score from the daily-run shared Chrome
— see TODO.md for the history that motivated this.

Usage:
    python scripts/bidfax_run.py --dir output \\
        --cache bidfax_cache.json --log logs/bidfax_deletions.json \\
        --workbook output/auction_results.xlsx
"""

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients import bidfax
from core.columns  import IN_PROGRESS, LOT_COL, MAKE_COL, PRICE_COL, VIN_COL
from core.csv_io   import find_price_files, load_csv_dict, save_csv_dict
from core.workbook import apply_result_to_row, resolve_columns

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl required. Install with: pip install openpyxl")


# ---------------------------------------------------------------------------
# Job structure
# ---------------------------------------------------------------------------

@dataclass
class _NewRowDest:
    """Append a freshly-built row to <out_path> at write time."""
    out_path: Path
    src_row:  dict
    out_fieldnames: list[str]


@dataclass
class _UpdateRowDest:
    """Mutate an existing row dict in place (file gets re-saved at end)."""
    path: Path
    row:  dict


@dataclass
class _WorkbookRowDest:
    """A workbook row needing in-place update (resolved later)."""
    sheet: str
    lot:   str  # used to find the row when applying


@dataclass
class BidfaxJob:
    lot:          str
    make:         str
    destinations: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 1 — queue builders
# ---------------------------------------------------------------------------

def _build_output_fieldnames(src_fieldnames: list[str]) -> list[str]:
    cols = list(src_fieldnames)
    insert_at = cols.index("Odometer") + 1 if "Odometer" in cols else len(cols)
    cols.insert(insert_at, PRICE_COL)
    cols.append(VIN_COL)
    return cols


def _read_search_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            sys.exit(f"Input CSV is empty: {path}")
        if LOT_COL not in reader.fieldnames:
            sys.exit(f"'{LOT_COL}' not found in {path.name}")
        return list(reader.fieldnames), list(reader)


def _filter_active_rows(rows: list[dict]) -> list[dict]:
    return [
        r for r in rows
        if str(r.get(MAKE_COL, "")).strip()
        and not str(r.get(MAKE_COL, "")).strip().startswith("#")
        and str(r.get(LOT_COL, "")).strip()
    ]


def _ensure_job(jobs: dict[str, BidfaxJob], lot: str, make: str) -> BidfaxJob:
    job = jobs.get(lot)
    if job is None:
        job = BidfaxJob(lot=lot, make=make)
        jobs[lot] = job
    elif not job.make and make:
        job.make = make
    return job


def collect_copart(
    jobs:        dict[str, BidfaxJob],
    input_path:  Path,
    output_path: Path,
    log_path:    Path,
    sale_ended:  dict[str, bool],
) -> tuple[list[dict], list[dict]]:
    """Build queue entries for today's Copart lots.

    Splits today's input into ended (queued for bidfax) and not-ended
    (deleted from input + appended to deletion log). Returns
    (active_rows, deleted_rows) for downstream output writing.
    """
    src_fieldnames, rows = _read_search_csv(input_path)
    active = _filter_active_rows(rows)
    out_fieldnames = _build_output_fieldnames(src_fieldnames)

    deleted: list[dict] = []
    kept:    list[dict] = []
    for row in active:
        url = str(row.get("Link", "")).strip()
        ended = sale_ended.get(url, False)
        if not ended:
            deleted.append(row)
            continue
        kept.append(row)
        lot  = str(row.get(LOT_COL, "")).strip()
        make = str(row.get(MAKE_COL, "")).strip()
        job  = _ensure_job(jobs, lot, make)
        job.destinations.append(_NewRowDest(output_path, row, out_fieldnames))

    if deleted:
        _append_deletion_log(log_path, input_path.name, deleted)
        deleted_lots = {str(r.get(LOT_COL, "")).strip() for r in deleted}
        _remove_from_input(input_path, src_fieldnames, rows, deleted_lots)

    return kept, deleted


def collect_iaai(
    jobs:        dict[str, BidfaxJob],
    input_path:  Path,
    output_path: Path,
) -> list[dict]:
    """Queue every active row from today's IAAI search."""
    src_fieldnames, rows = _read_search_csv(input_path)
    active = _filter_active_rows(rows)
    out_fieldnames = _build_output_fieldnames(src_fieldnames)
    for row in active:
        lot  = str(row.get(LOT_COL, "")).strip()
        make = str(row.get(MAKE_COL, "")).strip()
        job  = _ensure_job(jobs, lot, make)
        job.destinations.append(_NewRowDest(output_path, row, out_fieldnames))
    return active


def collect_inprogress_csvs(
    jobs:    dict[str, BidfaxJob],
    files:   list[Path],
) -> dict[Path, tuple[list[str], list[dict]]]:
    """Walk existing price CSVs; queue every In Progress row for refresh.

    Returns the loaded file data so phase 3 can re-save modified files.
    """
    file_data: dict[Path, tuple[list[str], list[dict]]] = {}
    for path in files:
        fieldnames, rows = load_csv_dict(path)
        if PRICE_COL not in fieldnames:
            print(f"  [skip] no '{PRICE_COL}' column: {path.name}")
            continue
        file_data[path] = (fieldnames, rows)
        for row in rows:
            if row.get(PRICE_COL, "").strip() != IN_PROGRESS:
                continue
            lot  = str(row.get(LOT_COL, "")).strip()
            make = str(row.get(MAKE_COL, "")).strip()
            if not lot:
                continue
            job = _ensure_job(jobs, lot, make)
            job.destinations.append(_UpdateRowDest(path, row))
    return file_data


def collect_inprogress_workbook(
    jobs:        dict[str, BidfaxJob],
    workbook_path: Path | None,
):
    """Open the workbook (if any), queue In Progress rows. Returns wb or None."""
    if workbook_path is None or not workbook_path.exists():
        return None
    wb = openpyxl.load_workbook(workbook_path)
    for ws in wb.worksheets:
        header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header or LOT_COL not in header or PRICE_COL not in header:
            continue
        lot_i   = list(header).index(LOT_COL)
        price_i = list(header).index(PRICE_COL)
        for row in ws.iter_rows(min_row=2, values_only=True):
            if str(row[price_i] or "").strip() != IN_PROGRESS:
                continue
            lot = str(row[lot_i] or "").strip()
            if not lot:
                continue
            job = _ensure_job(jobs, lot, ws.title)
            job.destinations.append(_WorkbookRowDest(ws.title, lot))
    return wb


# ---------------------------------------------------------------------------
# Deletion log + input rewrite (Copart-only)
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
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"[*] Logged {len(deleted_rows)} deletion(s) → {log_path.name}")


def _remove_from_input(
    input_path:    Path,
    src_fieldnames: list[str],
    rows:           list[dict],
    deleted_lots:   set[str],
) -> None:
    kept = [r for r in rows if str(r.get(LOT_COL, "")).strip() not in deleted_lots]
    with input_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=src_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)
    print(f"[*] Removed {len(rows) - len(kept)} lot(s) from {input_path.name}")


# ---------------------------------------------------------------------------
# Phase 2 — bidfax lookup (one Chrome, multi-tab)
# ---------------------------------------------------------------------------

def run_bidfax_lookup(
    jobs:           dict[str, BidfaxJob],
    cache_path:     Path,
    delay:          float,
    max_concurrent: int,
    client:         bidfax.BidfaxClient | None = None,
) -> dict[str, tuple[str, str, str]]:
    """Query bidfax for every lot in `jobs`. Returns {lot: (price, vin, url)}.

    Cached lots short-circuit. Only non-In-Progress results land in the cache.
    Browser is launched fresh (no --browser-port) — see header comment.
    """
    if not jobs:
        return {}
    queries = list(jobs.keys())
    makes   = {lot: job.make for lot, job in jobs.items()}
    return bidfax.run_batch(
        queries, delay, cache_path,
        makes=makes, browser_port=None, client=client,
        max_concurrent=max_concurrent,
    )


def run_sale_ended_check(
    lot_urls:    list[str],
    client:      bidfax.BidfaxClient | None = None,
) -> dict[str, bool]:
    """Hit each Copart lot URL; return {url: True if 'Sale ended' visible}."""
    if not lot_urls:
        return {}
    real_client = client or bidfax.BrowserBidfaxClient(browser_port=None)
    return real_client.check_sale_ended_many(lot_urls)


# ---------------------------------------------------------------------------
# Phase 3 — distribute results
# ---------------------------------------------------------------------------

def _apply_to_existing_row(row: dict, price: str, vin: str, url: str) -> None:
    row[PRICE_COL] = price
    if vin:
        row[VIN_COL] = vin
    if url and row.get("Link", "").strip() != url:
        row["Link"] = url


def _build_new_row(src_row: dict, price: str, vin: str, url: str) -> dict:
    row = dict(src_row)
    row[PRICE_COL] = price
    row[VIN_COL]   = vin
    if url:
        row["Link"] = url
    return row


def distribute(
    jobs:        dict[str, BidfaxJob],
    results:     dict[str, tuple[str, str, str]],
    file_data:   dict[Path, tuple[list[str], list[dict]]],
    wb,
) -> tuple[int, int]:
    """Write each lot's result into its destinations. Returns (csv, wb) counts."""
    new_rows_by_path: dict[Path, tuple[list[str], list[dict]]] = {}
    csv_updates = 0

    for lot, job in jobs.items():
        price, vin, url = results.get(lot, (IN_PROGRESS, "", ""))
        for dest in job.destinations:
            if isinstance(dest, _NewRowDest):
                bucket = new_rows_by_path.setdefault(
                    dest.out_path, (dest.out_fieldnames, []),
                )
                bucket[1].append(_build_new_row(dest.src_row, price, vin, url))
            elif isinstance(dest, _UpdateRowDest):
                if price == IN_PROGRESS:
                    continue   # skip refresh writes for unresolved lots
                _apply_to_existing_row(dest.row, price, vin, url)
                csv_updates += 1
            elif isinstance(dest, _WorkbookRowDest):
                pass   # handled in workbook pass below

    for out_path, (fieldnames, rows) in new_rows_by_path.items():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"[+] Wrote {len(rows)} row(s) → {out_path}")

    for path, (fieldnames, rows) in file_data.items():
        if any(_row_was_updated(r, results) for r in rows):
            save_csv_dict(path, fieldnames, rows)
            print(f"[+] Updated → {path.name}")

    wb_updates = _apply_workbook(wb, results) if wb is not None else 0
    return csv_updates, wb_updates


def _row_was_updated(row: dict, results: dict[str, tuple]) -> bool:
    lot = str(row.get(LOT_COL, "")).strip()
    if lot not in results:
        return False
    price = results[lot][0]
    return price != IN_PROGRESS and row.get(PRICE_COL, "").strip() == price


def _apply_workbook(wb, results: dict[str, tuple[str, str, str]]) -> int:
    updated = 0
    for ws in wb.worksheets:
        headers = list(next(ws.iter_rows(min_row=1, max_row=1, values_only=True), []))
        cols = resolve_columns(headers)
        if cols is None:
            continue
        for row in ws.iter_rows(min_row=2):
            lot = str(row[cols.lot - 1].value or "").strip()
            if lot not in results:
                continue
            price, vin, url = results[lot]
            if price == IN_PROGRESS:
                continue
            apply_result_to_row(row, cols, price, vin, url)
            updated += 1
    return updated


# ---------------------------------------------------------------------------
# Date resolution
# ---------------------------------------------------------------------------

def _find_recent_search(directory: Path, auction: str, date_str: str,
                        max_days: int = 7) -> str | None:
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process(
    work_dir:        Path,
    cache_path:      Path,
    log_path:        Path,
    workbook_path:   Path | None,
    delay:           float,
    max_concurrent:  int,
    copart_date:     str | None = None,
    iaai_date:       str | None = None,
    bidfax_client:   bidfax.BidfaxClient | None = None,
) -> None:
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y_%m_%d")
    cd = copart_date or _find_recent_search(work_dir, "copart", yesterday)
    id_ = iaai_date  or _find_recent_search(work_dir, "iaai",   yesterday)

    copart_input  = work_dir / f"copart_search_{cd}.csv" if cd  else None
    copart_output = work_dir / f"copart_price_{cd}.csv"  if cd  else None
    iaai_input    = work_dir / f"iaai_search_{id_}.csv"  if id_ else None
    iaai_output   = work_dir / f"iaai_price_{id_}.csv"   if id_ else None

    jobs: dict[str, BidfaxJob] = {}

    # ---- Phase 1a: Copart Sale-Ended check ------------------------------
    sale_ended: dict[str, bool] = {}
    if copart_input and copart_input.exists():
        _, raw_rows = _read_search_csv(copart_input)
        active_rows = _filter_active_rows(raw_rows)
        urls = [str(r.get("Link", "")).strip() for r in active_rows
                if str(r.get("Link", "")).strip()]
        print(f"[*] Sale-Ended check on {len(urls)} Copart lot(s)…")
        sale_ended = run_sale_ended_check(urls, bidfax_client)
        kept, deleted = collect_copart(jobs, copart_input, copart_output,
                                       log_path, sale_ended)
        print(f"[*] Copart: {len(kept)} ended → queued, "
              f"{len(deleted)} not-ended → removed")
    else:
        if copart_date is not None:
            print(f"[!] Copart input not found for date {copart_date}")

    # ---- Phase 1b: IAAI today's lots ------------------------------------
    if iaai_input and iaai_input.exists():
        active = collect_iaai(jobs, iaai_input, iaai_output)
        print(f"[*] IAAI: {len(active)} lot(s) → queued")
    else:
        if iaai_date is not None:
            print(f"[!] IAAI input not found for date {iaai_date}")

    # ---- Phase 1c: Stale In Progress (CSV + workbook) -------------------
    files = find_price_files(work_dir, "all")
    file_data = collect_inprogress_csvs(jobs, files)
    refresh_count = sum(1 for j in jobs.values()
                        if any(isinstance(d, _UpdateRowDest) for d in j.destinations))
    print(f"[*] Refresh: {refresh_count} stale In Progress row(s) → queued")

    wb = collect_inprogress_workbook(jobs, workbook_path)
    if wb is not None:
        print(f"[*] Workbook: scanned for In Progress rows")

    if not jobs:
        print("[+] Nothing to query — exiting.")
        return

    print(f"\n[*] Bidfax queue: {len(jobs)} unique lot(s)")

    # ---- Phase 2: bidfax lookup ----------------------------------------
    results = run_bidfax_lookup(
        jobs, cache_path, delay, max_concurrent, client=bidfax_client,
    )

    # ---- Phase 3: distribute -------------------------------------------
    csv_updates, wb_updates = distribute(jobs, results, file_data, wb)
    if wb is not None and wb_updates and workbook_path is not None:
        wb.save(workbook_path)
        print(f"[+] Workbook updated ({wb_updates} row(s)): {workbook_path.name}")

    print(f"\n[+] Done. CSV refresh updates: {csv_updates}, "
          f"workbook updates: {wb_updates}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consolidated bidfax pipeline — replaces "
                    "bidfax_info + price_refresh.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dir",         "-D", default="output",
                        help="Directory for input/output CSV files (default: output)")
    parser.add_argument("--cache",       "-c", default="bidfax_cache.json",
                        help="Cache file (default: bidfax_cache.json)")
    parser.add_argument("--log",         "-l", default="logs/bidfax_deletions.json",
                        help="Deletion log path (default: logs/bidfax_deletions.json)")
    parser.add_argument("--workbook",    "-w", default=None,
                        help="Excel workbook to update in-place (optional)")
    parser.add_argument("--copart-date", default=None,
                        help="Override Copart input date (yyyy_mm_dd)")
    parser.add_argument("--iaai-date",   default=None,
                        help="Override IAAI input date (yyyy_mm_dd)")
    parser.add_argument("--delay",       type=float, default=2.0,
                        help="Seconds between bidfax searches (default: 2.0)")
    parser.add_argument("--concurrent",  type=int,
                        default=bidfax.DEFAULT_TAB_CONCURRENCY,
                        help=f"Parallel bidfax tabs (default: "
                             f"{bidfax.DEFAULT_TAB_CONCURRENCY})")
    args = parser.parse_args()

    work_dir = Path(args.dir).resolve()
    process(
        work_dir       = work_dir,
        cache_path     = Path(args.cache),
        log_path       = Path(args.log),
        workbook_path  = Path(args.workbook) if args.workbook else None,
        delay          = args.delay,
        max_concurrent = args.concurrent,
        copart_date    = args.copart_date,
        iaai_date      = args.iaai_date,
    )


if __name__ == "__main__":
    main()
