"""IAAI filter-CSV parsing and equipment post-filter helpers.

Pure module — no browser, no asyncio. Lives next to `clients.iaai` so the
client module re-exports the same public surface (parse_filter_row,
read_filters_csv, equipment_matches, apply_equipment_postfilter) without
those helpers having to share a 800-line file with the BrowserIAAIClient.
"""

from __future__ import annotations

import re

from core.filters import apply_age_filter


def equipment_matches(full_title: str, equipment: str) -> bool:
    """Return True when every word in `equipment` appears as a token in `full_title`."""
    if not equipment:
        return True
    title_tokens   = set(re.findall(r'\S+', full_title.upper()))
    required_words = [w.upper() for w in equipment.split() if w.strip()]
    return all(word in title_tokens for word in required_words)


def _reassemble_segments(raw_line: str) -> list[str]:
    """Split a CSV row by commas, but rejoin segments that don't contain
    a colon onto the previous one — equipment values like "Premium Plus"
    contain commas and shouldn't be split."""
    segments: list[str] = []
    buffer = ""
    for part in raw_line.split(","):
        if ":" in part:
            if buffer:
                segments.append(buffer.strip())
            buffer = part
        else:
            buffer = (buffer + ", " + part) if buffer else part
    if buffer:
        segments.append(buffer.strip())
    return segments


def _apply_segment(filters: dict, key: str, val: str) -> None:
    if key == "make":
        filters["make"] = val.upper()
    elif key == "model":
        models = [v.strip().upper() for v in val.split(";") if v.strip()]
        filters["models"] = models if models else [val.upper()]
    elif key in ("year_min", "yearmin"):
        try: filters["year_min"] = int(val)
        except ValueError: pass
    elif key in ("year_max", "yearmax"):
        try: filters["year_max"] = int(val)
        except ValueError: pass
    elif key == "age":
        try: filters["age"] = int(val)
        except ValueError: pass
    elif key in ("odometer_max", "odo_max", "odometer"):
        try: filters["odometer_max"] = int(val)
        except ValueError: pass
    elif key in ("fuel_type", "fueltype", "fuel"):
        filters["fuel_type"] = val.strip()
    elif key == "equipment":
        filters["equipment"] = val.strip()


def parse_filter_row(raw_line: str) -> dict:
    """Parse one line of 'Key: Value' comma-separated segments."""
    filters: dict = {}
    for seg in _reassemble_segments(raw_line):
        m = re.match(r"^([^:]+?)\s*:\s*(.+)$", seg.strip())
        if not m:
            continue
        key = m.group(1).strip().lower().replace(" ", "_")
        _apply_segment(filters, key, m.group(2).strip())
    return apply_age_filter(filters)


def read_filters_csv(path: str) -> list[dict]:
    filter_list: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parsed = parse_filter_row(line)
            if parsed:
                filter_list.append(parsed)
    return filter_list


def apply_equipment_postfilter(page_records: list, equipment: str) -> list:
    """Drop scraped rows whose `_full_title` doesn't contain every word
    in `equipment` (in any order). The IAAI search panel doesn't have a
    trim filter, so we filter after scraping."""
    if not equipment:
        print(f"        -> {len(page_records)} raw vehicle(s) found on page", flush=True)
        return page_records
    kept = [r for r in page_records
            if equipment_matches(r.get("_full_title", ""), equipment)]
    dropped = len(page_records) - len(kept)
    print(f"        -> {len(page_records)} raw / {dropped} dropped by equipment "
          f"filter / {len(kept)} kept", flush=True)
    return kept
