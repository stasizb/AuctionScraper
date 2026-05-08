#!/usr/bin/env python3
"""
Remove rows from a source CSV whose Lot Number already exists in a destination CSV.

Usage:
    python remove_duplicates.py --src new.csv --dest existing.csv
    python remove_duplicates.py --src new.csv --dest existing.csv --backup true
"""

import argparse
import csv
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.csv_io import find_recent_search, load_csv_dict, save_csv_dict


LOT_COLUMN = "Lot Number"


def read_lot_numbers(path: Path) -> set[str]:
    fieldnames, rows = load_csv_dict(path)
    if LOT_COLUMN not in fieldnames:
        print(f"[!] '{LOT_COLUMN}' column not found in: {path}")
        sys.exit(1)
    return {row[LOT_COLUMN].strip() for row in rows if row.get(LOT_COLUMN, "").strip()}


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    fieldnames, rows = load_csv_dict(path)
    if LOT_COLUMN not in fieldnames:
        print(f"[!] '{LOT_COLUMN}' column not found in: {path}")
        sys.exit(1)
    return fieldnames, rows


def write_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    save_csv_dict(path, fieldnames, rows)


def remove_duplicate_lots(src_path: Path, dest_path: Path) -> int:
    """Remove rows from `src_path` whose Lot Number exists in `dest_path`.

    Rewrites `src_path` in place. Returns the count of removed rows.
    `dest_path` is not modified.
    """
    dest_lots        = read_lot_numbers(dest_path)
    fieldnames, rows = read_rows(src_path)
    kept       = [r for r in rows if r[LOT_COLUMN].strip() not in dest_lots]
    removed    = len(rows) - len(kept)
    if removed:
        write_rows(src_path, fieldnames, kept)
    return removed


def main() -> None:
    today     = date.today()
    yesterday = today - timedelta(days=1)

    parser = argparse.ArgumentParser(
        description="Remove rows from SRC whose Lot Number already exists in DEST.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--auction", "-a", default="copart",
                        help="Auction name: copart or iaai (default: copart)")
    parser.add_argument("--src",    default=None,
                        help="Source CSV to deduplicate (default: <auction>_search_yesterday.csv)")
    parser.add_argument("--dest",   default=None,
                        help="Destination CSV to check against (default: <auction>_search_today.csv)")
    parser.add_argument("--backup", default="false",
                        choices=["true", "false"],
                        help="Create a backup of SRC before modifying (default: false)")
    args = parser.parse_args()

    dest_path = Path(args.dest or f"{args.auction}_search_{today.strftime('%Y_%m_%d')}.csv")

    if args.src:
        src_path = Path(args.src)
    else:
        ds = find_recent_search(dest_path.parent, args.auction, yesterday)
        src_path = (dest_path.parent / f"{args.auction}_search_{ds}.csv"
                    if ds else
                    Path(f"{args.auction}_search_{yesterday.strftime('%Y_%m_%d')}.csv"))

    if not src_path.exists():
        print(f"[!] Source file not found: {src_path}")
        sys.exit(1)
    if not dest_path.exists():
        print(f"[!] Destination file not found: {dest_path}")
        sys.exit(1)

    dest_lots          = read_lot_numbers(dest_path)
    fieldnames, rows   = read_rows(src_path)

    duplicates = [r for r in rows if r[LOT_COLUMN].strip() in dest_lots]
    kept       = [r for r in rows if r[LOT_COLUMN].strip() not in dest_lots]

    print(f"[*] SRC rows      : {len(rows)}")
    print(f"[*] DEST lot numbers : {len(dest_lots)}")
    print(f"[*] Duplicates found : {len(duplicates)}")
    print(f"[*] Rows to keep     : {len(kept)}")

    if not duplicates:
        print("[+] No duplicates found — source file unchanged.")
        return

    if args.backup == "true":
        backup_path = src_path.with_suffix(".bak" + src_path.suffix)
        shutil.copy2(src_path, backup_path)
        print(f"[+] Backup created: {backup_path}")

    write_rows(src_path, fieldnames, kept)
    print(f"[+] Removed {len(duplicates)} duplicate(s) from: {src_path}")


if __name__ == "__main__":
    main()
