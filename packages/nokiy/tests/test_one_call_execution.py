# SPDX-License-Identifier: MIT
"""Fused CLI orchestration, strict evidence and original-ID recovery; no cloud calls."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack, model_topology, result_inspection


class OneCallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.output = self.root / "prepared"
        self.output.mkdir()
        self.path = self.output / "request.json"
        self.path.write_text('{}\n')
        self.request = SimpleNamespace(artifact_root=self.root,
            request_id="tura_embedded_" + "a" * 64,
            native_thread_id="00000000-0000-0000-0000-000000000001",
            authority_effect="workspace")
        self.prepared = {"status": "PREPARED", "request_id": self.request.request_id,
            "request": {"path": str(self.path), "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest()}}
        self.terminal = {"status": "RESULT_AVAILABLE", "request_id": self.request.request_id,
            "mission_acceptance": "parent_owned", "result_text": "answer"}
        self.argv = ["prepare", "--request", str(self.root / "draft.json"),
            "--action", str(self.root / "action.json"), "--output-dir", str(self.output)]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.prepare = self.stack.enter_context(patch.object(full_stack, "prepare_request", return_value=self.prepared))
        self.load = self.stack.enter_context(patch.object(model_topology, "load_prepared_request", return_value=(self.request, None)))
        self.execute = self.stack.enter_context(patch.object(caller, "execute", return_value=self.terminal))
        self.preflight = self.stack.enter_context(patch.object(caller, "preflight"))
        self.project = self.stack.enter_context(patch.object(result_inspection, "project_terminal",
            side_effect=lambda value, *a, **kw: {**value, "result_inspection": {"status": "EVIDENCE_VERIFIED"}}))
        self.retain = self.stack.enter_context(patch.object(result_inspection, "retain_inspection_summary",
            side_effect=lambda value, *a, **kw: value))

    def cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = caller.main(argv)
        return code, json.loads(out.getvalue())

    def test_prepare_only_preserves_no_execution(self):
        code, result = self.cli(self.argv)
        self.assertEqual((code, result["status"]), (0, "PREPARED"))
        self.execute.assert_not_called()
        self.load.assert_not_called()
        self.project.assert_not_called()

    def test_combined_executes_and_projects_once_without_extra_preflight(self):
        code, result = self.cli(self.argv + ["--run", "--require-verified-evidence"])
        self.assertEqual(code, 0)
        self.prepare.assert_called_once()
        self.execute.assert_called_once_with(self.request)
        self.project.assert_called_once()
        self.retain.assert_called_once()
        self.preflight.assert_not_called()
        self.assertEqual(result["prepared_request"], self.prepared["request"])
        self.assertEqual(result["mission_acceptance"], "parent_owned")
        self.assertNotIn("prepared_request", self.terminal)

    def test_summary_reuses_projection_and_keeps_recovery_locator(self):
        with patch.object(result_inspection, "summarize_terminal", side_effect=lambda r, *a: r) as summary:
            code, result = self.cli(self.argv + ["--run", "--summary"])
        self.assertEqual(code, 0)
        self.project.assert_called_once()
        summary.assert_called_once()
        self.assertIn("prepared_request", result)

    def test_strict_incomplete_exits_three_without_changing_terminal(self):
        self.project.side_effect = lambda r, *a, **kw: {**r, "result_inspection": {"status": "INCOMPLETE_EVIDENCE"}}
        code, result = self.cli(self.argv + ["--run", "--require-verified-evidence"])
        self.assertEqual((code, result["status"]), (3, "RESULT_AVAILABLE"))
        self.execute.assert_called_once()

    def test_legacy_run_keeps_available_exit_zero(self):
        self.project.side_effect = lambda r, *a, **kw: {**r, "result_inspection": {"status": "INCOMPLETE_EVIDENCE"}}
        code, _ = self.cli(["run", "--request", str(self.path)])
        self.assertEqual(code, 0)

    def test_strict_read_result_never_executes_or_prepares(self):
        # read-result reads a recorded terminal; without one it reports interrupted recovery.
        (self.root / self.request.request_id).mkdir()
        (self.root / self.request.request_id / "terminal.json").write_text("{}\n")
        with patch.object(caller, "read_terminal", return_value=self.terminal):
            code, result = self.cli(["read-result", "--artifact-root", str(self.root),
                "--request-id", self.request.request_id, "--require-verified-evidence"])
        self.assertEqual(code, 0)
        self.execute.assert_not_called()
        self.prepare.assert_not_called()
        self.retain.assert_not_called()
        self.assertEqual(result["inspection_summary_omission"], "READ_ONLY_RECOVERY")

    def test_strict_does_not_upgrade_blocked_terminal(self):
        self.terminal["status"] = "BLOCKED"
        code, result = self.cli(self.argv + ["--run", "--require-verified-evidence"])
        self.assertEqual((code, result["status"]), (2, "BLOCKED"))

    def test_run_only_flags_rejected_before_prepare(self):
        for flag in ("--summary", "--require-verified-evidence"):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                caller.main(self.argv + [flag])
            self.assertEqual(error.exception.code, 2)
        self.prepare.assert_not_called()
        self.execute.assert_not_called()

    def test_changed_prepared_bytes_reject_before_execution(self):
        self.path.write_text('{"changed":true}\n')
        code, result = self.cli(self.argv + ["--run"])
        self.assertEqual(code, 2)
        self.assertEqual(result["first_typed_blocker"], "NOKIY_PREPARED_REQUEST_DRIFT")
        self.execute.assert_not_called()

    def test_changed_prepared_request_id_rejects_before_execution(self):
        self.prepared["request_id"] = "tura_embedded_" + "b" * 64
        code, result = self.cli(self.argv + ["--run"])
        self.assertEqual(code, 2)
        self.assertEqual(result["first_typed_blocker"], "NOKIY_PREPARED_REQUEST_DRIFT")
        self.execute.assert_not_called()

    def test_preparation_failure_cannot_start_execution(self):
        self.prepare.side_effect = caller.EmbeddedNokiyError("FIXTURE_PREPARE_FAILED", "no admission")
        code, result = self.cli(self.argv + ["--run"])
        self.assertEqual((code, result["status"]), (2, "BLOCKED_PREEXECUTION"))
        self.execute.assert_not_called()

    def test_consumed_request_exception_preserves_uncertain_effects(self):
        def consumed(request):
            (request.artifact_root / request.request_id).mkdir()
            raise OSError("fixture interrupted after consumption")
        self.execute.side_effect = consumed
        code, result = self.cli(self.argv + ["--run"])
        self.assertEqual((code, result["status"]), (2, "EXECUTION_UNCERTAIN"))
        self.assertEqual(result["authority_effect"], "workspace")
        self.assertEqual(result["request_id"], self.request.request_id)
        self.assertFalse(result["automatic_retry_allowed"])
        self.assertIsNone(result["native_mutation_count"])
        self.assertEqual(result["recovery_command"], "read-result")
        self.execute.assert_called_once()

    def test_pre_execution_rejection_does_not_claim_effect(self):
        self.execute.side_effect = caller.EmbeddedNokiyError("SOURCE_DRIFT", "execution preflight failed")
        code, result = self.cli(self.argv + ["--run"])
        self.assertEqual((code, result["status"]), (2, "BLOCKED_PREEXECUTION"))
        self.assertIn("prepared_request", result)


    def test_absent_or_invalid_inspection_cannot_pass_strict_exit(self):
        for value in (None, [], "EVIDENCE_VERIFIED", {}):
            with self.subTest(value=value):
                self.project.side_effect = lambda r, *a, **kw: {**r, "result_inspection": value}
                code, result = self.cli(self.argv + ["--run", "--require-verified-evidence"])
                self.assertEqual(code, 3)
                self.assertEqual(result["status"], "RESULT_AVAILABLE")


if __name__ == "__main__":
    unittest.main()
