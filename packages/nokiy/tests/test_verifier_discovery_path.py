"""Pinned discovery executables supply their directories to a verifier's child search path."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import test_verifier_parent as fixture
from codex_collaboration_harness import embedded_nokiy as caller


class VerifierDiscoveryPathTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ParentVerifierPostimageTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rg = Path(temporary.name).resolve() / "ripgrep" / "bin" / "rg"
        self.rg.parent.mkdir(parents=True)
        self.rg.write_bytes(b"#!/bin/sh\n")
        self.rg.chmod(0o700)
        cat = Path("/bin/cat")
        self.f.contract["read_commands"] = {"roots": ["src"],
            "rg": {"path": str(self.rg), "sha256": caller._file_sha256(self.rg)},
            "cat": {"path": str(cat), "sha256": caller._file_sha256(cat)}}
        self.ambient = {"HOME": "/actual-home", "PATH": "/ambient-bin", "TMPDIR": "/ambient-tmp"}

    def test_pinned_rg_directory_joins_the_child_search_path(self):
        expected = {"HOME": "/actual-home", "PATH": os.pathsep.join((str(self.rg.parent), "/usr/bin", "/bin")),
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "LANG": "C.UTF-8",
                    "TMPDIR": "/scratch"}
        with patch.dict(os.environ, self.ambient, clear=True):
            self.assertTrue(self.f.invoke(expected_env=expected)["success"])
        child = self.f.last_spawn.call_args.kwargs["env"]
        self.assertEqual(shutil.which("rg", path=child["PATH"]), str(self.rg))

    def test_a_changed_pinned_tool_keeps_the_minimal_search_path(self):
        self.rg.write_bytes(b"#!/bin/sh\necho changed\n")
        expected = {"HOME": "/actual-home", "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1", "LANG": "C.UTF-8", "TMPDIR": "/scratch"}
        with patch.dict(os.environ, self.ambient, clear=True):
            self.assertTrue(self.f.invoke(expected_env=expected)["success"])


if __name__ == "__main__":
    unittest.main()
