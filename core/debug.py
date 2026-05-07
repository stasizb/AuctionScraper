"""Debug toggles, env-driven (see core/env.py for resolution order).

DEBUG_SCREENSHOTS — when True, the scrapers write a PNG per filter row
(IAAI) and per bidfax try, into `logs/`. Default False so normal runs
don't generate hundreds of MB of imagery; flip on in `.env` while
diagnosing missed lots / soft-blocks.
"""

from core.env import read_bool

ENV_VAR = "DEBUG_SCREENSHOTS"

DEBUG_SCREENSHOTS = read_bool(ENV_VAR, default=False)
