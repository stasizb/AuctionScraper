"""CSV and file-pattern helpers shared across price-handling scripts."""

from __future__ import annotations

import csv
import re
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
