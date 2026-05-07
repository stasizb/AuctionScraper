"""Shared env-var + .env loader for project config.

Resolution order for any config key:
  1. Process env var (set in shell / CI)
  2. .env file at the project root (committed default)
  3. Hardcoded fallback supplied by the caller

Designed to be imported by every config-reading module so we don't keep
duplicating the dotenv parser. Malformed values fall back silently — a bad
.env entry must never crash module import.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE     = PROJECT_ROOT / ".env"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}
_FALSY  = {"0", "false", "no", "off", "n", "f", ""}


def load_dotenv() -> dict[str, str]:
    """Parse the project-root .env file. Returns {KEY: VALUE} pairs.

    Format: one `KEY=value` per line. Lines starting with `#` and blanks
    are ignored. Surrounding single/double quotes are stripped.
    """
    if not ENV_FILE.is_file():
        return {}
    pairs: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        pairs[key.strip()] = val
    return pairs


def _get_raw(name: str) -> str | None:
    return os.environ.get(name) or load_dotenv().get(name)


def read_int(name: str, default: int, *, min_value: int = 1) -> int:
    """Read an int env-var. Falls back to `default` when unset, malformed,
    or less than `min_value` (so a zero/negative can't deadlock callers
    that use the result as a Semaphore size, etc.)."""
    raw = _get_raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= min_value else default


def read_bool(name: str, default: bool) -> bool:
    """Read a boolean env-var. Accepts 1/true/yes/on (truthy) and
    0/false/no/off/empty (falsy), case-insensitive. Anything else falls
    back to `default`."""
    raw = _get_raw(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    return default
