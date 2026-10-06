# SPDX-License-Identifier: MIT
"""Single-process preparation uses project APIs, never a second DCF authority."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_collaboration_harness import dcf_prepare as bridge
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import embedded_nokiy as caller


class DcfPrepareTests(unittest.TestCase):
    def setUp(self):
        self.action = {"navigation_targets": ["symbol:package.item"], "context_summary": "Inspect it.",
                       "operations": ["read"], "read_scopes": ["package.py"], "write_scopes": []}
        self.snapshot = SimpleNamespace(generation_id="generation-1")
        self.runtime = Mock()
        self.runtime.store.load_current.return_value = (self.snapshot, None, Path("/generation-1"))
        self.runtime.store.graph_paths.return_value = (Path("/overlay.sqlite"), Path("/base.sqlite"))
        self.capabilities = {"source-navigation": {"freshness_status": "current",
                                                  "projection_status": "pass", "domain_verdict": "pass"}}
        self.runtime.capability_views.return_value = (None, self.capabilities, None)
        graph = ModuleType("scripts.ops.dcf.graph")
        graph.resolve_entities = Mock(return_value=[{"entity_id": "symbol:package.item",
            "entity_type": "symbol", "payload": {"path": "package.py", "line": 7}}])
        jspace = ModuleType("scripts.ops.dcf.jspace")
        jspace.compile_jspace_contract = Mock(return_value={"dcf_generation": {"generation_id": "generation-1"}})
        jspace.verify_contract_freshness = Mock()
        jspace.compile_task_context_capsule = Mock(return_value={"canonical": True})
        self.graph, self.jspace = graph, jspace
        self.modules = patch.dict(sys.modules, {graph.__name__: graph, jspace.__name__: jspace})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.connect = patch.object(bridge.sqlite3, "connect", side_effect=lambda *a, **k: Mock()).start()
        self.addCleanup(patch.stopall)

    def test_one_generation_readonly_indexes_and_canonical_authority(self):
        original = copy.deepcopy(self.action)
        value = bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.assertEqual(self.action, original)
        self.assertEqual(value["action"]["read_scopes"], original["read_scopes"])
        self.assertEqual(value["action"]["write_scopes"], [])
        self.assertNotIn("navigation_targets", value["action"])
        self.assertIn("package.py:7", value["action"]["context_summary"])
        self.assertEqual(self.connect.call_count, 2)
        for call in self.connect.call_args_list:
            self.assertTrue(call.args[0].endswith("?mode=ro"))
        self.runtime.store.load_current.assert_called_once_with(verify_file_hashes=True)
        self.graph.resolve_entities.assert_called_once()
        self.jspace.compile_jspace_contract.assert_called_once_with(
            self.runtime, surface_id="fixture", action=value["action"])
        self.jspace.verify_contract_freshness.assert_called_once()

    def test_multiple_targets_share_connections_and_generation(self):
        self.action["navigation_targets"].append("symbol:package.other")
        self.graph.resolve_entities.side_effect = lambda con, target, **kw: [{
            "entity_id": target, "entity_type": "symbol", "payload": {"path": "package.py", "line": 8}}]
        value = bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.assertEqual(len(value["navigation_locators"]), 2)
        self.assertEqual(self.connect.call_count, 2)
        self.runtime.capability_views.assert_called_once()

    def test_stale_or_failed_navigation_never_compiles(self):
        for key in self.capabilities["source-navigation"]:
            original = self.capabilities["source-navigation"][key]
            self.capabilities["source-navigation"][key] = "stale"
            with self.assertRaises(ValueError):
                bridge.compile_bundle(self.runtime, "fixture", self.action)
            self.capabilities["source-navigation"][key] = original
        self.jspace.compile_jspace_contract.assert_not_called()
        self.connect.assert_not_called()

    def test_generation_drift_and_freshness_failure_never_return_capsule(self):
        self.jspace.compile_jspace_contract.return_value["dcf_generation"]["generation_id"] = "other"
        with self.assertRaises(ValueError):
            bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.jspace.compile_task_context_capsule.assert_not_called()
        self.jspace.compile_jspace_contract.return_value["dcf_generation"]["generation_id"] = "generation-1"
        self.jspace.verify_contract_freshness.side_effect = ValueError("changed required domain")
        with self.assertRaises(ValueError):
            bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.jspace.compile_task_context_capsule.assert_not_called()

    def test_fuzzy_ambiguous_non_symbol_and_unsafe_locations_rejected(self):
        row = self.graph.resolve_entities.return_value[0]
        for rows in ([], [row, row], [dict(row, entity_id="symbol:package.other")],
                     [dict(row, entity_type="path")],
                     [dict(row, payload={"path": "../outside", "line": 7})],
                     [dict(row, payload={"path": "package.py", "line": True})]):
            self.graph.resolve_entities.return_value = rows
            with self.assertRaises(ValueError):
                bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.jspace.compile_jspace_contract.assert_not_called()

    def test_transport_errors_do_not_retry_or_downgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            compiler = Path(temp) / "dcf.py"
            compiler.write_text("# fixture")
            for result in (subprocess.TimeoutExpired("fixture", 60), OSError("fixture"),
                           subprocess.CompletedProcess([], 2, "{}"),
                           subprocess.CompletedProcess([], 0, "{}"),
                           subprocess.CompletedProcess([], 0, "x" * (caller.MAX_REQUEST_BYTES + 1))):
                kwargs = ({"side_effect": result} if isinstance(result, Exception) else {"return_value": result})
                with patch.object(stack.subprocess, "run", **kwargs) as run:
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                        stack._compile_navigated_dcf(Path(temp), compiler, Path(sys.executable), "fixture", self.action)
                run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
