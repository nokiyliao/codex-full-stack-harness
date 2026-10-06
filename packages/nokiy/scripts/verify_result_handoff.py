# SPDX-License-Identifier: MIT
"""Run the focused public result-handoff regression suite in admitted scratch."""
import sys
from pathlib import Path
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "tests"))
suite = unittest.TestSuite()
for pattern in ("test_result_inspection.py", "test_capability_gap_envelopes.py", "test_verified_gap_projection.py"):
    suite.addTests(unittest.defaultTestLoader.discover(str(root / "tests"), pattern=pattern))
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
