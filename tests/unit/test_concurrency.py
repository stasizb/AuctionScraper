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


def _reload(env: dict[str, str] | None = None) -> int:
    """Reload core.concurrency under the given env, returning the new value."""
    env = env or {}
    with patch.dict(os.environ, env, clear=False):
        if "DEFAULT_TAB_CONCURRENCY" not in env:
            os.environ.pop("DEFAULT_TAB_CONCURRENCY", None)
        importlib.reload(concurrency_mod)
        return concurrency_mod.DEFAULT_TAB_CONCURRENCY


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
        os.environ.pop("DEFAULT_TAB_CONCURRENCY", None)
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

    def test_bidfax_reexports_default_tab_concurrency(self):
        from clients import bidfax
        self.assertEqual(bidfax.DEFAULT_TAB_CONCURRENCY,
                         concurrency_mod.DEFAULT_TAB_CONCURRENCY)


if __name__ == "__main__":
    unittest.main()
