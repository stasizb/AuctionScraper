"""Unit tests for core/concurrency.py — the shared scraper concurrency knob.

The constant is read at module-import time from (in order): the
DEFAULT_TAB_CONCURRENCY env var, then the project-root .env file, then a
hardcoded fallback. Tests reload the module after manipulating env / .env
to capture a fresh read.
"""

import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import ROOT  # noqa: F401

import core.concurrency as concurrency_mod
import core.env         as env_mod


_CONCURRENCY_VARS = (
    "DEFAULT_TAB_CONCURRENCY",
    "IAAI_TAB_CONCURRENCY",
    "BIDFAX_TAB_CONCURRENCY",
)


def _reload(env: dict[str, str] | None = None,
            attr: str = "DEFAULT_TAB_CONCURRENCY") -> int:
    """Reload core.concurrency under the given env, returning `attr`'s value.

    Any concurrency env var not present in `env` is cleared, so callers
    don't have to worry about stale state leaking between tests."""
    env = env or {}
    with patch.dict(os.environ, env, clear=False):
        for var in _CONCURRENCY_VARS:
            if var not in env:
                os.environ.pop(var, None)
        importlib.reload(concurrency_mod)
        return getattr(concurrency_mod, attr)


class _DotenvSandbox:
    """Temporarily replace the project .env file and restore it on exit."""

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


class TestDefaultTabConcurrency(unittest.TestCase):
    """Env-var path: process env > .env > default."""

    def tearDown(self):
        for var in _CONCURRENCY_VARS:
            os.environ.pop(var, None)
        importlib.reload(concurrency_mod)

    def test_unset_env_with_repo_dotenv_uses_dotenv_value(self):
        # When the env var is unset, the value must come from the repo's
        # .env file. Read it ourselves so this test stays correct as the
        # team default changes over time.
        os.environ.pop("DEFAULT_TAB_CONCURRENCY", None)
        expected = int(env_mod.load_dotenv()["DEFAULT_TAB_CONCURRENCY"])
        importlib.reload(concurrency_mod)
        self.assertEqual(concurrency_mod.DEFAULT_TAB_CONCURRENCY, expected)

    def test_env_var_overrides_default(self):
        self.assertEqual(_reload({"DEFAULT_TAB_CONCURRENCY": "5"}), 5)

    def test_malformed_env_var_falls_back_to_default(self):
        self.assertEqual(_reload({"DEFAULT_TAB_CONCURRENCY": "lots"}), 2)

    def test_zero_or_negative_falls_back_to_default(self):
        # Zero would deadlock the asyncio.Semaphore — guard against it.
        self.assertEqual(_reload({"DEFAULT_TAB_CONCURRENCY": "0"}),  2)
        self.assertEqual(_reload({"DEFAULT_TAB_CONCURRENCY": "-7"}), 2)


class TestDotenvFile(unittest.TestCase):
    """When the env var is unset, the .env file is consulted."""

    def setUp(self):
        os.environ.pop("DEFAULT_TAB_CONCURRENCY", None)

    def tearDown(self):
        os.environ.pop("DEFAULT_TAB_CONCURRENCY", None)
        importlib.reload(concurrency_mod)

    def test_dotenv_value_used_when_env_unset(self):
        with _DotenvSandbox("DEFAULT_TAB_CONCURRENCY=4\n"):
            importlib.reload(concurrency_mod)
            self.assertEqual(concurrency_mod.DEFAULT_TAB_CONCURRENCY, 4)

    def test_process_env_wins_over_dotenv(self):
        # Both set; the shell-set value must take precedence.
        with _DotenvSandbox("DEFAULT_TAB_CONCURRENCY=4\n"), \
             patch.dict(os.environ, {"DEFAULT_TAB_CONCURRENCY": "9"}):
            importlib.reload(concurrency_mod)
            self.assertEqual(concurrency_mod.DEFAULT_TAB_CONCURRENCY, 9)

    def test_missing_dotenv_falls_back_to_hardcoded_default(self):
        with _DotenvSandbox(None):  # delete the file
            importlib.reload(concurrency_mod)
            self.assertEqual(concurrency_mod.DEFAULT_TAB_CONCURRENCY, 2)

    def test_dotenv_ignores_comments_and_blanks(self):
        with _DotenvSandbox(
            "# leading comment\n"
            "\n"
            "DEFAULT_TAB_CONCURRENCY=3\n"
            "# trailing comment\n"
        ):
            importlib.reload(concurrency_mod)
            self.assertEqual(concurrency_mod.DEFAULT_TAB_CONCURRENCY, 3)

    def test_dotenv_strips_quotes(self):
        with _DotenvSandbox('DEFAULT_TAB_CONCURRENCY="6"\n'):
            importlib.reload(concurrency_mod)
            self.assertEqual(concurrency_mod.DEFAULT_TAB_CONCURRENCY, 6)


class TestRedirectedToClients(unittest.TestCase):
    """clients.iaai and clients.bidfax must surface the same constant."""

    def test_iaai_reexports_default_tab_concurrency(self):
        from clients import iaai as iaai_client
        self.assertEqual(iaai_client.DEFAULT_TAB_CONCURRENCY,
                         concurrency_mod.DEFAULT_TAB_CONCURRENCY)
        self.assertEqual(iaai_client.IAAI_TAB_CONCURRENCY,
                         concurrency_mod.IAAI_TAB_CONCURRENCY)

    def test_bidfax_reexports_default_tab_concurrency(self):
        from clients import bidfax
        self.assertEqual(bidfax.DEFAULT_TAB_CONCURRENCY,
                         concurrency_mod.DEFAULT_TAB_CONCURRENCY)
        self.assertEqual(bidfax.BIDFAX_TAB_CONCURRENCY,
                         concurrency_mod.BIDFAX_TAB_CONCURRENCY)


class TestPerSiteOverrides(unittest.TestCase):
    """IAAI_TAB_CONCURRENCY / BIDFAX_TAB_CONCURRENCY override the shared default
    but still fall back to it when unset.

    Every test below runs against a *clean* .env (via _DotenvSandbox) so any
    per-site override the developer happens to have committed locally can't
    leak into the test and mask a real bug."""

    def setUp(self):
        # Each test gets the same blank-.env sandbox so test outcomes don't
        # depend on what's in the project root .env.
        self._dotenv = _DotenvSandbox(None)
        self._dotenv.__enter__()

    def tearDown(self):
        self._dotenv.__exit__(None, None, None)
        for var in _CONCURRENCY_VARS:
            os.environ.pop(var, None)
        importlib.reload(concurrency_mod)

    def test_iaai_inherits_default_when_unset(self):
        self.assertEqual(
            _reload({"DEFAULT_TAB_CONCURRENCY": "5"}, attr="IAAI_TAB_CONCURRENCY"), 5)

    def test_bidfax_inherits_default_when_unset(self):
        self.assertEqual(
            _reload({"DEFAULT_TAB_CONCURRENCY": "5"}, attr="BIDFAX_TAB_CONCURRENCY"), 5)

    def test_iaai_override_does_not_leak_to_bidfax(self):
        # Setting IAAI must not change BIDFAX (and vice-versa); they're independent.
        self.assertEqual(
            _reload({"DEFAULT_TAB_CONCURRENCY": "2",
                     "IAAI_TAB_CONCURRENCY":    "7"},
                    attr="IAAI_TAB_CONCURRENCY"), 7)
        self.assertEqual(concurrency_mod.BIDFAX_TAB_CONCURRENCY, 2)
        self.assertEqual(concurrency_mod.DEFAULT_TAB_CONCURRENCY, 2)

    def test_bidfax_override_does_not_leak_to_iaai(self):
        self.assertEqual(
            _reload({"DEFAULT_TAB_CONCURRENCY": "2",
                     "BIDFAX_TAB_CONCURRENCY":  "8"},
                    attr="BIDFAX_TAB_CONCURRENCY"), 8)
        self.assertEqual(concurrency_mod.IAAI_TAB_CONCURRENCY, 2)

    def test_per_site_malformed_falls_back_to_default(self):
        # Bad IAAI value → falls back to DEFAULT, not the hardcoded 2, so a
        # team-wide DEFAULT bump still applies.
        self.assertEqual(
            _reload({"DEFAULT_TAB_CONCURRENCY": "3",
                     "IAAI_TAB_CONCURRENCY":    "lots"},
                    attr="IAAI_TAB_CONCURRENCY"), 3)

    def test_per_site_zero_falls_back_to_default(self):
        self.assertEqual(
            _reload({"DEFAULT_TAB_CONCURRENCY": "3",
                     "BIDFAX_TAB_CONCURRENCY":  "0"},
                    attr="BIDFAX_TAB_CONCURRENCY"), 3)


if __name__ == "__main__":
    unittest.main()
