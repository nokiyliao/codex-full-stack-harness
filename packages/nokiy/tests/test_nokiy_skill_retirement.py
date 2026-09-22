# SPDX-License-Identifier: MIT
from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from codex_collaboration_harness.native_nokiy import main


class NokiySkillRetirementTests(unittest.TestCase):
    def test_install_skill_command_is_not_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stderr = StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(["install-skill", "--codex-home", temporary])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("invalid choice", stderr.getvalue())
            self.assertFalse((Path(temporary) / "skills" / "tura-kernel").exists())


if __name__ == "__main__":
    unittest.main()
