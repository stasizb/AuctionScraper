"""Shared test helpers — import path setup + common fixture paths."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CSV_FIXTURES   = FIXTURES / "csv"
GOLDEN_FIXTURES = FIXTURES / "golden"
