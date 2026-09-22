# SPDX-License-Identifier: MIT
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from review_readiness import _check_ci, _check_pyproject, _is_excluded


class ReviewReadinessBoundaryTests(unittest.TestCase):
    def test_named_virtualenv_variants_are_excluded(self) -> None:
        self.assertTrue(_is_excluded(Path(".venv/lib/example.py")))
        self.assertTrue(_is_excluded(Path(".venv312/lib/example.py")))
        self.assertTrue(_is_excluded(Path("nested/.venv-ci/bin/python")))

    def test_generated_packaging_metadata_is_excluded(self) -> None:
        self.assertTrue(_is_excluded(Path("src/example_package.egg-info/PKG-INFO")))

    def test_public_source_remains_in_scope(self) -> None:
        self.assertFalse(_is_excluded(Path("src/codex_collaboration_harness/core.py")))

    def test_release_supply_chain_is_pinned(self) -> None:
        errors: list[str] = []
        _check_ci(errors)
        self.assertEqual(errors, [])

    def test_project_metadata_is_review_ready(self) -> None:
        errors: list[str] = []
        _check_pyproject(errors)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
