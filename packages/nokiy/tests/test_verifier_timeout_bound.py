# SPDX-License-Identifier: MIT
"""Focused verifier grants accept a bound of up to one hour, not the former five minutes."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from codex_collaboration_harness import dcf_prepare, embedded_nokiy as caller, local_context


def action(reads, command):
    return {"mission": {"mission_id": "verifier-timeout", "task_id": "bound", "mode": "DELIVERY",
                        "objective": "Run a long focused verifier", "current_predicate": "Bound accepted"},
            "context_summary": "Disposable exact-scope fixture; no provider sends.",
            "operations": ["read", "command"], "source_read": True,
            "read_scopes": reads, "write_scopes": [], "target_paths": [],
            "command_templates": [], "forbidden_effects": ["create", "modify", "delete", "network"],
            "verifier_commands": [command]}


class VerifierTimeoutBoundTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "subject.txt").write_text("subject\n")
        self.artifacts = root / "artifacts"
        (self.artifacts / "scratch").mkdir(parents=True)
        self.executable = root / "python3.13"
        self.executable.write_bytes(bytes.fromhex("cffaedfe") + b"offline; never spawned\n")
        self.executable.chmod(0o755)

    def compile(self, timeout):
        command = {"argv": [str(self.executable), "-B", "-m", "unittest", "test_subject"],
                   "executable_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                   "pinned_files": [], "timeout_seconds": timeout, "network": False,
                   "scratch_root": str(self.artifacts / "scratch"), "cwd": str(self.workspace),
                   "python_import_roots": [str(Path(__file__).resolve().parent)]}
        return local_context.compile_context(self.workspace, action(["subject.txt"], command),
                                             artifact_root=self.artifacts)

    def test_grant_keeps_its_bound_up_to_one_hour(self):
        self.assertEqual(dcf_prepare.VERIFIER_TIMEOUT_MAX_SECONDS, 3600)
        for timeout in (1, 300, 301, 1800, 3600):
            _, contract = self.compile(timeout)
            self.assertEqual(contract["verifier_commands"][0]["timeout_seconds"], timeout)
        for timeout in (0, 3601, True, 1.5, "300"):
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.compile(timeout)


if __name__ == "__main__":
    unittest.main()
