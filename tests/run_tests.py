#!/usr/bin/env python3
"""Discover and run every test under tests/ using the stdlib unittest runner.

Usage:
    python tests/run_tests.py               # all tests (quiet)
    python tests/run_tests.py -v             # verbose
    python tests/run_tests.py unit           # only unit tests
    python tests/run_tests.py integration    # only integration tests
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def main() -> int:
    verbose  = "-v" in sys.argv or "--verbose" in sys.argv
    pattern  = "test_*.py"
    start    = "tests"
    for arg in sys.argv[1:]:
        if arg in ("unit", "integration"):
            start = f"tests/{arg}"

    loader = unittest.TestLoader()
    suite  = loader.discover(start_dir=str(ROOT / start), pattern=pattern,
                             top_level_dir=str(ROOT))

    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1, buffer=True)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
