#!/usr/bin/env python3
"""
IAAI.com scraper — thin CLI wrapper around clients.iaai.BrowserIAAIClient.

Usage:
    python scripts/iaai_search.py [--input input.csv] [--output output.csv]

Input CSV format (key: value pairs, comma-separated, one search per line):
    Make: Lincoln, Model: Nautilus, Year min: 2024, Year max: 2027, Odometer max: 30000
    Make: Mercedes-Benz, Model: Gle, Year min: 2022, Year max: 2025, Odometer max: 25000, Fuel Type: Hybrid Engine, Equipment: 4MATIC
    Make: Audi, Model: Q5, Year min: 2021, Year max: 2024, Odometer max: 40000, Equipment: Premium 45

Equipment post-filter:
    All words in the Equipment value must appear in the vehicle's full title,
    in any order (case-insensitive).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients import iaai as iaai_client


def process(
    input_path: str,
    output_path: str,
    profile_dir: str = "caches/chrome_profile_iaai",
    browser_port: int | None = None,
    client: iaai_client.IAAIClient | None = None,
) -> None:
    """Run the full IAAI scrape pipeline. `client` may be injected for tests."""
    print(f"[*] Reading filters from: {input_path}")
    filter_rows = iaai_client.read_filters_csv(input_path)
    if not filter_rows:
        print("[!] No filter rows found in input CSV. Exiting.")
        return
    print(f"[*] {len(filter_rows)} filter set(s) to process.")

    real_client = client or iaai_client.BrowserIAAIClient(
        browser_port=browser_port, profile_dir=profile_dir,
    )

    all_results: list[dict] = []
    try:
        for idx, filters in enumerate(filter_rows, 1):
            print(f"\n{'='*60}")
            print(f"[*] Filter set {idx}/{len(filter_rows)}")
            try:
                records = real_client.scrape_with_filters(filters, clear_filters=(idx > 1))
                all_results.extend(records)
            except Exception as exc:
                print(f"[!] Error on filter set {idx}: {exc}")
                import traceback
                traceback.print_exc()
    finally:
        # BrowserIAAIClient has its own teardown; fakes no-op.
        if hasattr(real_client, "_stop"):
            pass  # cleanup happens if the client is a context manager elsewhere

    iaai_client.write_output_csv(output_path, all_results)
    print(f"\n[+] Done. Total vehicles saved: {len(all_results)}")


def cli() -> None:
    from datetime import date
    today = date.today().strftime("%Y_%m_%d")

    parser = argparse.ArgumentParser(
        description="Scrape IAAI.com with filters defined in a CSV file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input",  "-i", default="iaai_filters.csv",
                        help="Input CSV path (default: iaai_filters.csv)")
    parser.add_argument("--output", "-o", default=f"iaai_search_{today}.csv",
                        help=f"Output CSV path (default: iaai_search_{today}.csv)")
    parser.add_argument("--profile-dir",  "-p", default="caches/chrome_profile_iaai",
                        help="Persistent Chrome profile directory (default: caches/chrome_profile_iaai)")
    parser.add_argument("--browser-port", type=int, default=None,
                        help="Connect to a running Chrome on this port instead of launching one")
    args = parser.parse_args()

    if not Path(args.input).exists():
        sample = (
            "# One search per line - use 'Key: Value' pairs separated by commas\n"
            "# Separate multiple models with semicolons:  Model: Corsair;Nautilus\n"
            "# Equipment: all words must appear in the title (any order)\n"
            "#\n"
            "Make: Lincoln, Model: Nautilus, Year min: 2024, Year max: 2027, Odometer max: 30000\n"
            "Make: Audi, Model: Q5, Year min: 2021, Year max: 2024, Odometer max: 40000, Equipment: Premium 45\n"
            "Make: Mercedes-Benz, Model: Gle, Year min: 2022, Year max: 2025, "
            "Odometer max: 25000, Fuel Type: Hybrid Engine, Equipment: 4MATIC\n"
        )
        Path(args.input).write_text(sample, encoding="utf-8")
        print(f"[!] '{args.input}' not found - created a sample file. Edit it and re-run.")
        sys.exit(0)

    process(args.input, args.output, args.profile_dir, args.browser_port)


if __name__ == "__main__":
    cli()
