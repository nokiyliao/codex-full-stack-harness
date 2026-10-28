"""DCF data copied without the DCF program prepares through exact local grants."""
from unittest.mock import patch
import unittest

import test_local_context as base
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import local_context as local

RULE = {"path": "workspace", "rule": "dcf_data_without_program_to_local_context"}


class DcfDataWithoutProgramTests(unittest.TestCase):
    def setUp(self):
        self.base = base.LocalContextTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.f = self.base.f
        contract = self.f.workspace / "config/contracts/dcf_v2_contract.json"
        contract.parent.mkdir(parents=True)
        contract.write_text("{}\n")
        self.base.install_jspace_compiler(self.f.workspace)

    def test_snapshot_prepares_local_context_and_passes_the_execution_boundary(self):
        self.assertEqual(local.dcf_root(self.f.workspace), self.f.workspace)
        self.assertIsNone(local.jspace_root(self.f.workspace))
        with patch.object(caller, "execute", side_effect=AssertionError("no model call")):
            result = self.base.prepare()
        self.assertIn(RULE, result["input_adaptations"])
        self.assertFalse(result["provider_execution_started"])
        caller.preflight(caller.load_request(self.base.output / "request.json"))

    def test_surface_requests_are_dropped_for_the_local_route(self):
        with patch.object(caller, "execute", side_effect=AssertionError("no model call")):
            result = self.base.prepare("research_execution_engine")
        self.assertIn(RULE, result["input_adaptations"])
        self.assertIn({"path": "surface_id", "rule": "local_context_needs_no_surface"},
                      result["input_adaptations"])

    def test_installing_the_program_later_is_rejected_at_execution(self):
        self.base.prepare()
        (self.f.workspace / "scripts/ops/dcf.py").write_text("# installed later\n")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_INVALID"):
            caller.preflight(caller.load_request(self.base.output / "request.json"))

    def test_a_present_program_keeps_the_no_downgrade_refusal(self):
        program = self.f.workspace / "scripts/ops/dcf.py"
        for kind in ("file", "broken_link"):
            with self.subTest(kind=kind):
                if program.exists() or program.is_symlink():
                    program.unlink()
                if kind == "file":
                    program.write_text("# DCF\n")
                else:
                    program.symlink_to("/nonexistent-dcf")
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"):
                    self.base.prepare()
                self.assertFalse(self.base.output.exists())

    def test_surface_query_reports_the_local_route(self):
        result = stack.query_surface_candidates(self.f.workspace, ["input.txt"])
        self.assertEqual(result["status"], "CONTEXT_ROUTE_AVAILABLE")
        self.assertEqual(result["dcf_state"], "data_without_program")
        self.assertFalse(result["surface_required"])


if __name__ == "__main__":
    unittest.main()
