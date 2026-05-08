"""Unit tests for core/logging_setup.py.

setup_logging is process-global, so each test snapshots and restores
root-logger state to avoid bleeding handlers across tests.
"""

import logging
import tempfile
import unittest
from pathlib import Path

from tests._helpers import ROOT  # noqa: F401

from core.logging_setup import setup_logging, _OWNED_ATTR


class _RootLoggerSnapshot:
    def __enter__(self):
        root = logging.getLogger()
        self._handlers = list(root.handlers)
        self._level    = root.level
        # Strip any handlers a previous test left, so each test starts clean.
        for h in list(root.handlers):
            root.removeHandler(h)
        return self

    def __exit__(self, *_):
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in self._handlers:
            root.addHandler(h)
        root.setLevel(self._level)


class TestSetupLogging(unittest.TestCase):
    def test_installs_console_handler(self):
        with _RootLoggerSnapshot():
            setup_logging()
            stream_handlers = [h for h in logging.getLogger().handlers
                               if isinstance(h, logging.StreamHandler)
                               and not isinstance(h, logging.FileHandler)
                               and getattr(h, _OWNED_ATTR, False)]
            self.assertEqual(len(stream_handlers), 1)

    def test_idempotent(self):
        with _RootLoggerSnapshot():
            setup_logging()
            setup_logging()
            setup_logging()
            owned = [h for h in logging.getLogger().handlers
                     if getattr(h, _OWNED_ATTR, False)]
            # Console handler installed exactly once even after 3 calls.
            self.assertEqual(len(owned), 1)

    def test_file_handler_writes_to_log_file(self):
        with _RootLoggerSnapshot(), tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "subdir" / "run.log"
            setup_logging(log_file=log_file)
            log = logging.getLogger("auction_scraper.test")
            log.info("hello")
            for h in logging.getLogger().handlers:
                h.flush()
            text = log_file.read_text(encoding="utf-8")
            self.assertIn("hello", text)
            self.assertIn("auction_scraper.test", text)

    def test_file_handler_creates_parent_directory(self):
        with _RootLoggerSnapshot(), tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "a" / "b" / "c" / "run.log"
            setup_logging(log_file=nested)
            self.assertTrue(nested.parent.is_dir())


if __name__ == "__main__":
    unittest.main()
