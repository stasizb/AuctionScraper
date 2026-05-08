"""CSV and file-pattern helpers shared across price-handling scripts."""

from __future__ import annotations

import csv
import re
from datetime import date, timedelta
from pathlib import Path


# Matches <auction>_price_<yyyy>_<mm>_<dd>.csv — the "priced" output files.
PRICE_FILE_PATTERN = re.compile(
    r"^(iaai|copart)_price_(\d{4})_(\d{2})_(\d{2})\.csv$",
    re.IGNORECASE,
)


def load_csv_dict(path: Path) -> tuple[list[str], list[dict]]:
    """Read a CSV as (fieldnames, list-of-row-dicts). BOM-safe."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader     = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows       = list(reader)
    return fieldnames, rows


def save_csv_dict(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """Write row dicts to CSV, ignoring extra keys not in fieldnames."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def find_price_files(directory: Path, auction: str = "all") -> list[Path]:
    """Return sorted `<auction>_price_<date>.csv` paths in `directory`.

    `auction` is "copart", "iaai", or "all".
    """
    files: list[Path] = []
    for path in sorted(directory.glob("*.csv")):
        m = PRICE_FILE_PATTERN.match(path.name)
        if m and (auction == "all" or m.group(1).lower() == auction.lower()):
            files.append(path)
    return files


def find_recent_search(
    directory:  Path,
    auction:    str,
    on_or_before: date | str,
    *,
    max_days:   int = 7,
) -> str | None:
    """Return the most recent `<auction>_search_<YYYY_MM_DD>.csv` date string
    on or before `on_or_before`, walking back up to `max_days` days.

    `on_or_before` can be a `datetime.date` or a "YYYY_MM_DD" / "YYYY-MM-DD"
    string. The returned value is always the "YYYY_MM_DD" string used in
    file names, or None if no matching file exists in that window.

    Consolidates the four near-identical implementations that previously
    lived in run_daily.py, bidfax_info.py, bidfax_run.py, and
    remove_duplicates.py.
    """
    if isinstance(on_or_before, str):
        try:
            on_or_before = date.fromisoformat(on_or_before.replace("_", "-"))
        except ValueError:
            return None
    for offset in range(max_days):
        candidate = on_or_before - timedelta(days=offset)
        path = directory / f"{auction}_search_{candidate.strftime('%Y_%m_%d')}.csv"
        if path.exists():
            return candidate.strftime("%Y_%m_%d")
    return None
