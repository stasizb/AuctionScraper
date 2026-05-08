"""On-disk JSON cache for bidfax lookups.

The cache stores `{lot_or_vin: (price, vin, url)}` entries plus a reserved
`_ts` map of `{key: "YYYY-MM-DD"}` per-entry write timestamps. New writes
go through `cache_results`, which stamps each entry; loads with `ttl_days`
silently drop entries older than that. Legacy entries without a timestamp
are treated as fresh — rolling TTL out doesn't nuke existing caches.

Pure module — no browser, no asyncio.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from core.env import read_int

# Sentinel meaning "the bidfax lookup hasn't returned a final price yet".
# Mirrored from core.columns to keep this module self-contained for tests
# that import it without the rest of the bidfax stack.
IN_PROGRESS = "In Progress"

# Reserved cache key holding a {entry_key: "YYYY-MM-DD"} timestamp map.
_TIMESTAMPS_KEY = "_ts"

# How many days a cached bidfax result stays valid. Override via the
# BIDFAX_CACHE_TTL_DAYS env / .env entry; 0 disables expiration.
DEFAULT_CACHE_TTL_DAYS = read_int("BIDFAX_CACHE_TTL_DAYS", 60, min_value=0)


def _expire_old_entries(cache: dict, ttl_days: int, today: date) -> dict:
    """Drop cache entries whose timestamp is older than `ttl_days`.
    Legacy entries (no timestamp recorded) are kept — treat them as fresh
    so we don't lose months of accumulated lookups on TTL rollout."""
    timestamps = cache.get(_TIMESTAMPS_KEY)
    if not isinstance(timestamps, dict) or not timestamps:
        return cache
    cutoff_iso = (today - timedelta(days=ttl_days)).isoformat()
    expired = [k for k, ts in timestamps.items()
               if isinstance(ts, str) and ts < cutoff_iso]
    if not expired:
        return cache
    for k in expired:
        cache.pop(k, None)
        timestamps.pop(k, None)
    return cache


def load_cache(
    path: Path,
    *,
    ttl_days: int | None = None,
    today:    date | None = None,
) -> dict:
    """Read the bidfax cache. With `ttl_days` set, expired entries are
    silently dropped on read; the on-disk file is rewritten the next time
    the caller saves. `today` is injectable for tests."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    cache: dict = {}
    for k, v in data.items():
        if k == _TIMESTAMPS_KEY:
            cache[k] = dict(v) if isinstance(v, dict) else {}
        elif isinstance(v, list):
            cache[k] = tuple(v)
        else:
            cache[k] = v
    if ttl_days is not None and ttl_days > 0:
        cache = _expire_old_entries(cache, ttl_days, today or date.today())
    return cache


def save_cache(path: Path, cache: dict) -> None:
    serialisable = {k: (list(v) if isinstance(v, tuple) else v)
                    for k, v in cache.items()}
    path.write_text(json.dumps(serialisable, indent=2, sort_keys=True),
                    encoding="utf-8")


def cache_results(
    cache_path: Path,
    fetched:    dict[str, tuple[str, str, str]],
    *,
    today:      date | None = None,
) -> dict[str, tuple]:
    """Merge `fetched` into the on-disk cache, persisting only final
    (non-IN_PROGRESS) results. Each new entry gets today's timestamp so
    `load_cache(..., ttl_days=N)` can expire it later.

    Use this from any code path that calls `BidfaxClient.lookup_many`
    directly (e.g. price_fix re-fetch) so the next caller benefits from
    the resolved prices and bidfax doesn't get a duplicate query.
    """
    cache = load_cache(cache_path)
    timestamps: dict = cache.setdefault(_TIMESTAMPS_KEY, {})
    today_iso = (today or date.today()).isoformat()
    for q, v in fetched.items():
        if v[0] == IN_PROGRESS:
            continue
        cache[q] = v
        timestamps[q] = today_iso
    save_cache(cache_path, cache)
    return cache
