"""Shared helpers for normalizing filter dicts produced by parse_filter_row.

Both copart and IAAI filter parsers produce the same shape of filter dict, so
post-processing rules that span multiple keys live here.
"""

from datetime import date


def apply_age_filter(filters: dict, today: date | None = None) -> dict:
    """Translate an 'age' filter into a year_min, dropping year_max.

    Age has higher priority than explicit year_min / year_max — when both
    are present in the same row, age wins. `today` is injectable for tests.
    """
    if "age" not in filters:
        return filters
    age = filters.pop("age")
    if not isinstance(age, int):
        return filters
    today = today or date.today()
    filters["year_min"] = today.year - age
    filters.pop("year_max", None)
    return filters
