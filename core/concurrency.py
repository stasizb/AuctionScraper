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

from core.env import read_int, load_dotenv as _load_dotenv  # noqa: F401  (back-compat)
from core.env import ENV_FILE     as _ENV_FILE              # noqa: F401  (back-compat)

ENV_VAR  = "DEFAULT_TAB_CONCURRENCY"
_DEFAULT = 2

DEFAULT_TAB_CONCURRENCY = read_int(ENV_VAR, _DEFAULT, min_value=1)
