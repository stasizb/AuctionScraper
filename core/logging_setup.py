"""Project logging configuration.

Sets up the root logger with a console handler and (optionally) a file
handler. Designed to coexist with `core.job_log`: the print() calls used
inside parallel async workers still flow through the job_log proxy and
get buffered per-task. Top-level orchestration (run_daily.py and the
scripts) can opt into structured logging via `log = logging.getLogger(__name__)`
for things that benefit from level filtering or a persistent run log.

`setup_logging()` is idempotent — repeated calls won't stack handlers.
"""

from __future__ import annotations

import logging
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Tag we stick on every handler this module installs so we can recognise
# (and skip) ones we already own — makes setup_logging() idempotent across
# re-imports / re-entries.
_OWNED_ATTR = "_auction_scraper_managed"


def _own(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _OWNED_ATTR, True)
    return handler


def _has_managed_handler_of_type(t: type) -> bool:
    return any(isinstance(h, t) and getattr(h, _OWNED_ATTR, False)
               for h in logging.getLogger().handlers)


def setup_logging(
    level:    int  = logging.INFO,
    log_file: Path | None = None,
) -> None:
    """Configure the root logger. Safe to call multiple times.

    `log_file` (when given) opens an append-mode handler — the daily run
    can pass `logs/run_<date>.log` so a full transcript persists even if
    the terminal was closed.
    """
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if not _has_managed_handler_of_type(logging.StreamHandler):
        sh = _own(logging.StreamHandler())
        sh.setFormatter(formatter)
        sh.setLevel(level)
        root.addHandler(sh)

    if log_file is not None and not _has_managed_file_handler(log_file):
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = _own(logging.FileHandler(log_file, mode="a", encoding="utf-8"))
        fh.setFormatter(formatter)
        fh.setLevel(level)
        root.addHandler(fh)


def _has_managed_file_handler(path: Path) -> bool:
    target = str(path.resolve())
    for h in logging.getLogger().handlers:
        if (isinstance(h, logging.FileHandler)
                and getattr(h, _OWNED_ATTR, False)
                and Path(h.baseFilename).resolve() == Path(target)):
            return True
    return False
