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


LOT_COLUMN = "Lot Number"


def _find_recent_search(directory: Path, auction: str, before: date, max_days: int = 7) -> Path | None:
    """Return the path of the most recent <auction>_search_<date>.csv on or before `before`."""
    for offset in range(max_days):
        candidate = before - timedelta(days=offset)
        path = directory / f"{auction}_search_{candidate.strftime('%Y_%m_%d')}.csv"
        if path.exists():
            return path
    return None


def read_lot_numbers(path: Path) -> set[str]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if LOT_COLUMN not in (reader.fieldnames or []):
            print(f"[!] '{LOT_COLUMN}' column not found in: {path}")
            sys.exit(1)
        return {row[LOT_COLUMN].strip() for row in reader if row[LOT_COLUMN].strip()}


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        if LOT_COLUMN not in fieldnames:
            print(f"[!] '{LOT_COLUMN}' column not found in: {path}")
            sys.exit(1)
        return fieldnames, list(reader)


def write_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
        src_path = _find_recent_search(dest_path.parent, args.auction, yesterday) or \
                   Path(f"{args.auction}_search_{yesterday.strftime('%Y_%m_%d')}.csv")

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
