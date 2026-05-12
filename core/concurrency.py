"""Shared scraper concurrency settings.

DEFAULT_TAB_CONCURRENCY governs how many parallel browser tabs the scrapers
(IAAI search, bidfax lookup) open against an auction site at once.

Per-site overrides (IAAI_TAB_CONCURRENCY, BIDFAX_TAB_CONCURRENCY) let one
auction be tuned without affecting the other — useful when, e.g., bidfax
tolerates 4 tabs while IAAI rate-limits past 2.

Resolution order for each constant (first hit wins):
  1. Process env var (set in shell / CI)
  2. .env file at the project root
  3. The fallback shown below

Malformed values, zero, or negatives all silently fall back so a
misconfigured .env can never deadlock the asyncio.Semaphore.
"""

from core.env import read_int

ENV_VAR  = "DEFAULT_TAB_CONCURRENCY"
_DEFAULT = 2

DEFAULT_TAB_CONCURRENCY = read_int(ENV_VAR, _DEFAULT, min_value=1)
# Per-site overrides fall back to the shared default, so setting only
# DEFAULT_TAB_CONCURRENCY still controls both sites.
IAAI_TAB_CONCURRENCY    = read_int("IAAI_TAB_CONCURRENCY",   DEFAULT_TAB_CONCURRENCY, min_value=1)
BIDFAX_TAB_CONCURRENCY  = read_int("BIDFAX_TAB_CONCURRENCY", DEFAULT_TAB_CONCURRENCY, min_value=1)
