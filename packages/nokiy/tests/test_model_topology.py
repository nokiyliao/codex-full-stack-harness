# SPDX-License-Identifier: MIT
"""Offline selection tests. These make NO claims about live model dispatch."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import model_topology as topology


def catalog_fixture():
    return {"models": [
        {"slug": "gpt-6.1-sol" if name == "sol" else "gpt-6-" + name,
         "supported_reasoning_levels": [{"effort": "max"}] +
         ([{"effort": "ultra"}] if name != "luna" else []),
         "context_window": 100000 + i * 1000, "max_context_window": 200000 + i * 1000,
         "model_messages": {"instructions_template": "fixture " + name},
         "additional_speed_tiers": []}
        for i, name in enumerate(topology.FAMILIES)
    ]}


class ModelTopologyTests(unittest.TestCase):
    def assert_blocked(self, code, function, *args):
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, "NOKIY_TOPOLOGY_" + code)

    def test_all_six_selections_keep_commander_and_worker_independent(self):
        for mode in topology.MODES:
            for family in topology.FAMILIES:
                with self.subTest(mode=mode, family=family):
                    result = topology.resolve(mode, family)
                    model = "gpt-6.1-sol" if family == "sol" else "gpt-6-" + family
                    self.assertEqual(result["worker"], {"model": model, "reasoning_effort": "max"})
                    self.assertEqual(result["commander"], None if mode == "nokiy" else
                                     {"model": "gpt-6-astra", "reasoning_effort": "ultra"})
                    topology.require_catalog_support(result, catalog_fixture())
                    self.assertFalse(result["selection_is_authorization"])
                    self.assertFalse(result["worker_delegation_allowed"])

    def test_invalid_selector_is_not_silently_downgraded(self):
        for family in ["ultra", "high", "auto", "SOL", "", {}, 0]:
            self.assert_blocked("FAMILY_INVALID", topology.resolve, "nokiy", family)
        for mode in ["gpt-6-sol", "nokiy/direct", {}, None]:
            self.assert_blocked("MODE_INVALID", topology.resolve, mode, "sol")

    def test_default_family_is_sol_and_resolutions_do_not_share_mutable_state(self):
        value = topology.resolve("nokiy-direct")
        value["worker"]["model"] = "changed"
        self.assertEqual(topology.resolve("nokiy-direct")["worker"]["model"], "gpt-6.1-sol")

    def test_explicit_worker_settings_cannot_be_silently_overridden(self):
        for draft in [{"model": "gpt-6-sol"}, {"reasoning_effort": "ultra"},
                      {"execution_profile": "balanced"}, {"model_provider": "other"},
                      {"request_id": "previous"}, {"jspace_contract": {}}]:
            self.assert_blocked("REQUEST_CONFLICT", topology.project_worker_draft, draft, "nokiy", "luna")

    def test_projection_preserves_all_authority_and_service_fields(self):
        draft = {"native_thread_id": "owner", "authority_effect": "none", "allow_provider_network": False,
                 "service_tier": "priority", "workspace": "/workspace", "prompt": "bounded task",
                 "timeout_seconds": 45, "runtime_image": {"path": "/image", "sha256": "x"}}
        original = deepcopy(draft)
        projected, _ = topology.project_worker_draft(draft, "nokiy-direct", "luna")
        self.assertEqual(draft, original)
        self.assertEqual({key: projected[key] for key in draft}, original)
        self.assertEqual(projected["model"], "gpt-6-luna")
        self.assertEqual(projected["reasoning_effort"], "max")
        self.assertNotIn("commander", projected)

    def test_missing_model_or_effort_rejected_before_execution(self):
        for mode, family in [("nokiy", "luna"), ("nokiy-direct", "sol")]:
            catalog = catalog_fixture()
            target = "gpt-6-luna" if mode == "nokiy" else "gpt-6-astra"
            catalog["models"] = [row for row in catalog["models"] if row["slug"] != target]
            self.assert_blocked("MODEL_UNAVAILABLE", topology.require_catalog_support,
                                topology.resolve(mode, family), catalog)
        catalog = catalog_fixture()
        catalog["models"][2]["supported_reasoning_levels"] = [{"effort": "max"}]
        self.assert_blocked("MODEL_UNAVAILABLE", topology.require_catalog_support,
                            topology.resolve("nokiy-direct", "sol"), catalog)

    def test_duplicate_and_malformed_catalog_rejected(self):
        catalog = catalog_fixture()
        catalog["models"].append(deepcopy(catalog["models"][0]))
        self.assert_blocked("CATALOG_INVALID", topology.build_candidate_catalog, catalog)
        for catalog in [{}, {"models": [None]}, {"models": [{"slug": []}]}]:
            self.assert_blocked("CATALOG_INVALID", topology.build_candidate_catalog, catalog)

    def test_candidate_has_two_entries_and_preserves_original_models(self):
        catalog = catalog_fixture()
        original = deepcopy(catalog)
        output = topology.build_candidate_catalog(catalog)
        self.assertEqual(catalog, original)
        self.assertEqual(output["models"][:-2], original["models"])
        self.assertEqual([row["slug"] for row in output["models"][-2:]], list(topology.MODES))
        for row in output["models"][-2:]:
            self.assertEqual([level["effort"] for level in row["supported_reasoning_levels"]], list(topology.FAMILIES))
            self.assertEqual(row["default_reasoning_level"], "sol")
            self.assertEqual(row["context_window"], 100000)
        output["models"][-1]["model_messages"]["instructions_template"] = "changed"
        self.assertEqual(catalog, original)

    def test_candidate_cannot_duplicate_existing_alias(self):
        self.assert_blocked("CATALOG_CONFLICT", topology.build_candidate_catalog,
                            topology.build_candidate_catalog(catalog_fixture()))

    def test_cli_candidate_cannot_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            output = Path(directory) / "candidate.json"
            source.write_text(json.dumps(catalog_fixture()))
            with patch("builtins.print"):
                self.assertEqual(topology.main(["catalog-candidate", "--catalog", str(source), "--output", str(output)]), 0)
                before = output.read_bytes()
                self.assertEqual(topology.main(["catalog-candidate", "--catalog", str(source), "--output", str(output)]), 2)
            self.assertEqual(output.read_bytes(), before)

    def test_plan_cli_has_no_provider_or_execution_side_effect(self):
        with patch.object(caller, "execute") as execute, patch("builtins.print") as output:
            self.assertEqual(topology.main(["plan", "--model", "nokiy-direct", "--worker-family", "astra"]), 0)
        execute.assert_not_called()
        value = json.loads(output.call_args.args[0])
        self.assertEqual(value["status"], "RESOLVED_NOT_EXECUTED")


if __name__ == "__main__":
    unittest.main()
