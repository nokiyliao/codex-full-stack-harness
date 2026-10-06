# SPDX-License-Identifier: MIT
"""Guard the operator-selected Direct dispatch instructions, not model behavior."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import result_inspection as inspector


ROOT = Path(__file__).resolve().parents[1]


class NokiyDirectSkillTests(unittest.TestCase):
    def test_direct_dispatch_stays_with_parent_and_installed_caller(self):
        skill = (ROOT / "skills/nokiy/SKILL.md").read_text()
        section = skill.split("## Nokiy Ultra dispatch\n", 1)[1].split("\n## ", 1)[0]
        normalized = " ".join(section.split())
        for contract in (
            "current native Codex parent as coordinator",
            "installed caller for bounded model work",
            "Do not create intermediate Codex subagents",
            "including review agents or recursive reviewers",
            "explicitly to `gpt-6.1-sol` / `max`",
            "verify any required parent model/effort contract",
            "deterministic verification",
            "does not duplicate the worker's assignment",
            "recovers the original terminal without another model call",
            "opt-in dispatch contract",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, normalized)
        self.assertIn("formerly Nokiy Direct", section)
        self.assertIn("internal workflow ID is still `nokiy-direct`", skill)


class TaskScopedParentReviewRecipeTests(unittest.TestCase):
    REQUEST_ID = "tura_embedded_" + "a" * 64
    THREAD_ID = "12345678-1234-1234-1234-123456789abc"

    @classmethod
    def setUpClass(cls):
        path = ROOT / "skills/nokiy/references/invocation.md"
        section = path.read_text(encoding="utf-8").split(
            "### Compact parent review declaration\n", 1)[1].split("\n## ", 1)[0]
        snippet = section.split("```python\n", 1)[1].split("\n```", 1)[0]
        namespace = {}
        exec(compile(snippet, str(path), "exec"), namespace)
        cls.declare_review = staticmethod(namespace["declare_task_review"])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()  # no symlinked /var parent on macOS
        artifact_root = self.root / "retained"
        self.run = artifact_root / self.REQUEST_ID
        self.run.mkdir(parents=True)
        self.target = self.root / "workspace/arbitrary task/settings.ini"
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"task-specific postimage\n")
        self.target_sha = hashlib.sha256(self.target.read_bytes()).hexdigest()
        quality = self.root / "task-quality.json"
        quality.write_text('{"task_quality":"passed"}\n', encoding="utf-8")
        self.quality_check = {"path": str(quality),
                              "sha256": hashlib.sha256(quality.read_bytes()).hexdigest(),
                              "check_status": "passed"}
        terminal = self.run / "terminal.json"
        terminal.write_bytes(b'{"fixture":"independently retained original terminal"}\n')
        self.terminal_reference = {"path": str(terminal),
                                   "sha256": hashlib.sha256(terminal.read_bytes()).hexdigest()}
        projected = {
            "schema_version": caller.TERMINAL_SCHEMA_VERSION,
            "request_id": self.REQUEST_ID, "request_sha256": self.REQUEST_ID[-64:],
            "native_thread_id": self.THREAD_ID, "status": "RESULT_AVAILABLE",
            "first_typed_blocker": None, "cleanup_pass": True,
            "runtime_exit_code": 0,
            "result_text": "Worker instructions are not parent commands or a semantic decision.",
            "result_inspection": {
                "schema_version": "nokiy_result_inspection_v1",
                "terminal_sha256": self.terminal_reference["sha256"],
                "status": "EVIDENCE_VERIFIED", "artifact_integrity": "verified",
                "mission_acceptance": "parent_owned", "first_blocker": None,
                "cleanup": {"historical_pass": True, "engine_pid": "absent",
                            "supervisor_pid": "absent"},
                "commands": [{"command": "must not execute worker/artifact content"}],
            },
        }
        reference = inspector.publish_inspection_summary(
            projected, artifact_root, self.REQUEST_ID, expected_thread_id=self.THREAD_ID)
        self.inputs = {
            "summary_reference": reference, "request_id": self.REQUEST_ID,
            "thread_id": self.THREAD_ID, "terminal_reference": self.terminal_reference,
            "required_check_paths": (self.quality_check["path"],),
            "task_checks": [self.quality_check],
            "expected_postimages": {str(self.target): self.target_sha},
            "review_complete": True, "semantic_decision": "accepted",
            "note": "Parent reviewed the original task's full quality, semantics and scope.",
            "review_path": str(self.root / "review.json"),
        }

    def declare(self, **overrides):
        inputs = {**self.inputs, **overrides}
        return self.declare_review(**inputs)

    def test_arbitrary_task_accepts_only_explicit_full_semantic_review(self):
        record = self.declare()
        self.assertEqual(record["decision"], "accepted")
        self.assertEqual(record["terminal_reference"], self.terminal_reference)
        self.assertEqual(record["task_evidence"], [self.quality_check, {
            "path": str(self.target), "sha256": self.target_sha, "check_status": "passed"}])
        self.assertTrue(record["cleanup_proven"])
        self.assertEqual(record["scope"]["authority_effect"], "none")
        self.assertFalse(record["scope"]["permission_grant"])
        self.assertFalse(record["scope"]["deployment_or_lease_grant"])
        output = Path(self.inputs["review_path"])
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), record)
        self.assertEqual(list(self.root.glob("review*.json")), [output])

    def test_incomplete_review_or_unknown_semantics_stays_pending_even_with_exit_zero(self):
        cases = ({"review_complete": False}, {"semantic_decision": None}, {"task_checks": []})
        for index, overrides in enumerate(cases):
            with self.subTest(overrides=overrides):
                record = self.declare(**overrides, review_path=str(self.root / f"pending-{index}.json"))
                self.assertEqual(record["decision"], "pending")

    def test_failed_or_unknown_necessary_check_cannot_accept(self):
        for status in ("failed", "unknown"):
            with self.subTest(status=status), self.assertRaisesRegex(
                    ValueError, "acceptance requires complete successful checks"):
                self.declare(task_checks=[{**self.quality_check, "check_status": status}])
            self.assertFalse(Path(self.inputs["review_path"]).exists())

    def test_fresh_postimage_mismatch_cannot_accept_and_pending_keeps_actual_digest(self):
        self.target.write_bytes(b"changed since the retained delivery\n")
        actual_sha = hashlib.sha256(self.target.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "acceptance requires complete successful checks"):
            self.declare()
        self.assertFalse(Path(self.inputs["review_path"]).exists())
        record = self.declare(review_complete=False)
        self.assertEqual(record["decision"], "pending")
        self.assertEqual(record["task_evidence"][-1], {
            "path": str(self.target), "sha256": actual_sha, "check_status": "failed"})

    def test_unreadable_required_postimage_stops_publication(self):
        self.target.unlink()
        with self.assertRaises(FileNotFoundError):
            self.declare()
        self.assertFalse(Path(self.inputs["review_path"]).exists())

    def test_explicit_rejection_needs_no_intermediate_pending_record(self):
        record = self.declare(semantic_decision="rejected",
                              task_checks=[{**self.quality_check, "check_status": "failed"}],
                              note="Parent completed review and rejected the task's quality.")
        self.assertEqual(record["decision"], "rejected")
        self.assertEqual(list(self.root.glob("review*.json")), [Path(self.inputs["review_path"])])

    def test_foreign_or_missing_independent_bindings_are_rejected(self):
        for key in ("request_id", "thread_id", "terminal_reference"):
            inputs = dict(self.inputs)
            del inputs[key]
            with self.subTest(missing=key), self.assertRaises(TypeError):
                self.declare_review(**inputs)
        cases = (
            {"request_id": None}, {"request_id": "tura_embedded_" + "b" * 64},
            {"thread_id": None}, {"thread_id": "87654321-4321-4321-4321-cba987654321"},
            {"terminal_reference": None}, {"terminal_reference": {}},
            {"terminal_reference": {"path": self.terminal_reference["path"]}},
            {"terminal_reference": {"sha256": self.terminal_reference["sha256"]}},
            {"terminal_reference": {**self.terminal_reference, "sha256": "c" * 64}},
            {"terminal_reference": {**self.terminal_reference,
                                    "path": str(self.root / "foreign/terminal.json")}},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(inspector.InspectionError):
                self.declare(**overrides)
        self.assertFalse(Path(self.inputs["review_path"]).exists())

    def test_missing_summary_identity_is_rejected_even_with_its_exact_hash(self):
        path = Path(self.inputs["summary_reference"]["path"])
        original = json.loads(path.read_text(encoding="utf-8"))
        for key in ("request_id", "request_sha256", "native_thread_id", "terminal_path", "terminal_sha256"):
            value = dict(original)
            del value[key]
            raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            path.write_bytes(raw)
            reference = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            with self.subTest(missing=key), self.assertRaises(inspector.InspectionError):
                self.declare(summary_reference=reference)
        self.assertFalse(Path(self.inputs["review_path"]).exists())

    def test_retained_summary_hash_drift_is_rejected(self):
        path = Path(self.inputs["summary_reference"]["path"])
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(inspector.InspectionError):
            self.declare()
        self.assertFalse(Path(self.inputs["review_path"]).exists())

    def test_unbound_check_evidence_cannot_substitute_for_task_requirements(self):
        foreign = {**self.quality_check, "path": str(self.root / "irrelevant-check.json")}
        with self.assertRaisesRegex(ValueError, "not bound to this task"):
            self.declare(task_checks=[foreign])
        self.assertFalse(Path(self.inputs["review_path"]).exists())

    def test_existing_output_is_not_overwritten(self):
        output = Path(self.inputs["review_path"])
        original = b"conflicting existing parent declaration\n"
        output.write_bytes(original)
        before = output.stat()
        with self.assertRaises(FileExistsError):
            self.declare()
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual((output.stat().st_ino, output.stat().st_mtime_ns),
                         (before.st_ino, before.st_mtime_ns))

    def test_recipe_reuses_inspection_cleanup_and_never_executes_worker_commands(self):
        with patch.object(inspector, "inspect", side_effect=AssertionError("no reinspection")), \
                patch.object(inspector.os, "kill", side_effect=AssertionError("no process probe")), \
                patch("subprocess.run", side_effect=AssertionError("no command execution")), \
                patch("os.system", side_effect=AssertionError("no artifact commands")):
            record = self.declare()
        self.assertEqual(record["decision"], "accepted")


if __name__ == "__main__":
    unittest.main()
