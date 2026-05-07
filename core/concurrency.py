"""Shared scraper concurrency settings.

DEFAULT_TAB_CONCURRENCY governs how many parallel browser tabs the scrapers
(IAAI search, bidfax lookup) open against an auction site at once.

Resolution order (first hit wins):
  1. Process env var DEFAULT_TAB_CONCURRENCY
  2. .env file at the project root, line `DEFAULT_TAB_CONCURRENCY=N`
  3. Fallback default (2)

Malformed values, zero, or negatives all silently fall back to the default
so a misconfigured .env can never deadlock the asyncio.Semaphore.
"""

import os
from pathlib import Path

ENV_VAR  = "DEFAULT_TAB_CONCURRENCY"
_DEFAULT = 2

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE     = _PROJECT_ROOT / ".env"


def _load_dotenv() -> dict[str, str]:
    """Parse the project-root .env file. Returns {KEY: VALUE} pairs.

    Format: one `KEY=value` per line. Lines starting with `#` and blanks
    are ignored. Surrounding single/double quotes are stripped.
    """
    if not _ENV_FILE.is_file():
        return {}
    pairs: dict[str, str] = {}
    for raw in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        pairs[key.strip()] = val
    return pairs


def _read_default() -> int:
    raw = os.environ.get(ENV_VAR) or _load_dotenv().get(ENV_VAR)
    if raw is None:
        return _DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT
    return value if value >= 1 else _DEFAULT


DEFAULT_TAB_CONCURRENCY = _read_default()
