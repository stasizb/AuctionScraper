"""Unit tests for core/debug.py — the DEBUG_SCREENSHOTS env flag.

Mirrors the structure of test_concurrency.py: each test reloads the module
under a controlled env so a stray DEBUG_SCREENSHOTS in the developer's
shell can't make the suite flaky.
"""

import asyncio
import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import ROOT  # noqa: F401

import core.debug as debug_mod
import core.env   as env_mod


def _reload(env: dict[str, str] | None = None) -> bool:
    env = env or {}
    with patch.dict(os.environ, env, clear=False):
        if "DEBUG_SCREENSHOTS" not in env:
            os.environ.pop("DEBUG_SCREENSHOTS", None)
        importlib.reload(debug_mod)
        return debug_mod.DEBUG_SCREENSHOTS


class _DotenvSandbox:
    def __init__(self, contents: str | None):
        self._contents = contents
        self._target   = Path(env_mod.ENV_FILE)
        self._backup   = self._target.with_suffix(".env.bak-test")

    def __enter__(self):
        if self._target.exists():
            self._target.rename(self._backup)
        if self._contents is not None:
            self._target.write_text(self._contents, encoding="utf-8")
        return self

    def __exit__(self, *_):
        if self._target.exists():
            self._target.unlink()
        if self._backup.exists():
            self._backup.rename(self._target)


class TestDebugScreenshotsFlag(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("DEBUG_SCREENSHOTS", None)
        importlib.reload(debug_mod)

    def test_unset_env_defaults_to_false(self):
        with _DotenvSandbox(None):
            os.environ.pop("DEBUG_SCREENSHOTS", None)
            importlib.reload(debug_mod)
            self.assertFalse(debug_mod.DEBUG_SCREENSHOTS)

    def test_truthy_env_values_enable_screenshots(self):
        # Any common-sense truthy form must enable the flag — operators
        # shouldn't have to guess the exact string.
        for raw in ("1", "true", "True", "TRUE", "yes", "on", "y", "t"):
            with self.subTest(raw=raw):
                self.assertTrue(_reload({"DEBUG_SCREENSHOTS": raw}))

    def test_falsy_env_values_disable_screenshots(self):
        for raw in ("0", "false", "False", "no", "off", "n", "f", ""):
            with self.subTest(raw=raw):
                self.assertFalse(_reload({"DEBUG_SCREENSHOTS": raw}))

    def test_garbage_env_value_falls_back_to_default(self):
        # A typo like "tru" or random text must not silently flip behavior.
        self.assertFalse(_reload({"DEBUG_SCREENSHOTS": "tru"}))
        self.assertFalse(_reload({"DEBUG_SCREENSHOTS": "lol"}))

    def test_dotenv_value_used_when_env_unset(self):
        os.environ.pop("DEBUG_SCREENSHOTS", None)
        with _DotenvSandbox("DEBUG_SCREENSHOTS=true\n"):
            importlib.reload(debug_mod)
            self.assertTrue(debug_mod.DEBUG_SCREENSHOTS)

    def test_process_env_wins_over_dotenv(self):
        with _DotenvSandbox("DEBUG_SCREENSHOTS=true\n"), \
             patch.dict(os.environ, {"DEBUG_SCREENSHOTS": "false"}):
            importlib.reload(debug_mod)
            self.assertFalse(debug_mod.DEBUG_SCREENSHOTS)


class TestScreenshotHelpersGated(unittest.TestCase):
    """Both screenshot helpers must short-circuit when the flag is False —
    no save_screenshot call, no logs/ touch, no print line."""

    def test_iaai_helper_noops_when_flag_false(self):
        from clients import iaai

        calls = {"shots": 0}
        class FakePage:
            async def save_screenshot(self, *a, **kw):
                calls["shots"] += 1

        with patch.object(iaai, "DEBUG_SCREENSHOTS", False):
            asyncio.run(iaai._save_search_screenshot(FakePage(), 0, "HONDA"))
        self.assertEqual(calls["shots"], 0)

    def test_iaai_helper_fires_when_flag_true(self):
        from clients import iaai

        calls = {"shots": 0}
        class FakePage:
            async def save_screenshot(self, *a, **kw):
                calls["shots"] += 1

        with patch.object(iaai, "DEBUG_SCREENSHOTS", True):
            asyncio.run(iaai._save_search_screenshot(FakePage(), 0, "HONDA"))
        self.assertEqual(calls["shots"], 1)

    def test_bidfax_helper_noops_when_flag_false(self):
        from clients import bidfax

        calls = {"shots": 0}
        class FakePage:
            async def save_screenshot(self, *a, **kw):
                calls["shots"] += 1

        with patch.object(bidfax, "DEBUG_SCREENSHOTS", False):
            asyncio.run(bidfax._save_query_screenshot(FakePage(), "44638833"))
        self.assertEqual(calls["shots"], 0)

    def test_bidfax_helper_fires_when_flag_true(self):
        from clients import bidfax

        calls = {"shots": 0}
        class FakePage:
            async def save_screenshot(self, *a, **kw):
                calls["shots"] += 1

        with patch.object(bidfax, "DEBUG_SCREENSHOTS", True):
            asyncio.run(bidfax._save_query_screenshot(FakePage(), "44638833"))
        self.assertEqual(calls["shots"], 1)


if __name__ == "__main__":
    unittest.main()
