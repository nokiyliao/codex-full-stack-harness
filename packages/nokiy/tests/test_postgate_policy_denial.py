# SPDX-License-Identifier: MIT
"""Native denial -> checked inspection -> admitted final gate, without providers."""
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_post_batch_verifier as gate_fixtures
import test_result_inspection as evidence_fixtures
from codex_collaboration_harness import batch, result_inspection as inspector


def denial_event():
    # An integration run supplies the JSON emitted by the Rust producer test.
    path = os.environ.get("NOKIY_POLICY_DENIAL_FIXTURE")
    if path:
        return json.loads(Path(path).read_text())
    return {"type": "item.completed", "item": {
        "id": "item_11", "type": "command_execution",
        "command": '{"path":"task_a/test_source_sections_extension.py","start_line":1,"end_line":80}',
        "aggregated_output": "JSPACE_SOURCE_READ_PATH_DENIED: operation=read target=task_a/test_source_sections_extension.py detail=SOURCE_READ_TARGET_INVALID: No such file or directory (os error 2)",
        "exit_code": None, "status": "failed", "command_type": "jspace",
        "runtime_id": "runtime-policy-denial", "step": 1, "command_index": 3,
        "command_id": "runtime-policy-denial.tool.command_run:call_denied:3",
        "command_run_id": "runtime-policy-denial.tool.command_run", "provider_tool_call_id": "call_denied",
        "policy_denial": {"jspace_error_code": "JSPACE_SOURCE_READ_PATH_DENIED", "operation": "read",
            "target": "task_a/test_source_sections_extension.py", "effect_state": "not_started",
            "mutation_count": 0, "authority_effect": "none", "delivery_state": "deterministic_policy_denial",
            "replayable": True}}}


class PostgatePolicyDenialTests(unittest.TestCase):
    def setUp(self):
        self.evidence = evidence_fixtures.InspectionTests()
        self.evidence.setUp()
        self.addCleanup(self.evidence.doCleanups)

    def fixture(self, mutate=None, *, denial=True, changed=True):
        def arrange(state):
            if denial:
                event = denial_event()
                state["events"].insert(0, event)
                state["contract"]["source_read"] = True
                state["contract"]["read_scopes"].append(event["item"]["policy_denial"]["target"])
            else:
                state["passed"]["aggregated_output"].pop("verification_evidence")
            if mutate:
                mutate(state)
        state = self.evidence.resolution_fixture(arrange, changed=changed)
        terminal = state["terminal"]
        terminal.update(runtime_exit_code=0, fallback_used=False)
        evidence_fixtures.store(state["run"] / "terminal.json", terminal)
        return state

    def inspect(self, state):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": evidence_fixtures.THREAD}):
            return inspector.project_terminal(state["terminal"], self.evidence.root,
                state["terminal"]["request_id"], request_thread_id=evidence_fixtures.THREAD,
                require_request_binding=True)

    def gate_fixture(self, projected, *, cancelled=False):
        gate = gate_fixtures.PostBatchVerifierTests()
        gate.setUp()
        self.addCleanup(gate.doCleanups)
        gate.workers[0].request_id = projected["request_id"]
        gate.members[0]["request_id"] = projected["request_id"]
        gate.save_post_plan()
        first = gate.workers[0].request_id
        original_execute = gate.execute
        def execute(request):
            terminal = original_execute(request)
            if request.request_id == first:
                if cancelled:
                    gate_fixtures.caller._active_cancellation.get().append(15)
                return deepcopy(projected)
            return dict(terminal, runtime_exit_code=0, fallback_used=False)
        def project(member, terminal):
            if member.request_id == first:
                return deepcopy(projected)
            return {**terminal, "result_inspection": {"status": "EVIDENCE_VERIFIED",
                "first_blocker": None, "artifact_integrity": "verified"}}
        gate.exe.side_effect = execute
        return gate, project

    def test_producer_witness_keeps_unavailable_failure_and_allows_final_gate_once(self):
        state = self.fixture()
        projected = self.inspect(state)
        inspection = projected["result_inspection"]
        self.assertEqual(inspection["status"], "INCOMPLETE_EVIDENCE")
        self.assertEqual(inspection["first_blocker"], "COMMAND_FAILURE")
        denied = next(row for row in inspection["commands"] if row["event_index"] == 0)
        self.assertEqual((denied["execution_proof"], denied["receipt_check"]), ("unavailable", "unavailable"))
        self.assertNotIn("failure_resolution", denied)
        self.assertEqual(inspection["verifier_failure_resolution"]["resolved_count"], 1)
        self.assertEqual(inspection["post_batch_verifier_settlement"]["policy_denials"], 1)
        self.assertEqual(inspection["post_batch_verifier_settlement"]["failed"], 2)
        gate, project = self.gate_fixture(projected)
        with patch.object(batch, "_project", side_effect=project):
            result = batch.run_batch(gate.plan, gate.digest)
            self.assertEqual(result["post_batch_verifier"]["status"], "PASSED")
            self.assertEqual((result["status"], result["first_typed_blocker"]), ("BLOCKED", "COMMAND_FAILURE"))
            self.assertEqual(result["members"][0]["terminal"]["result_inspection"], inspection)
            recovered = batch.run_batch(gate.plan, gate.digest)
            self.assertEqual(recovered["post_batch_verifier"]["status"], "UNKNOWN")
            self.assertEqual(recovered["status"], "BLOCKED")
        self.assertEqual((gate.exe.call_count, gate.gate_call.call_count), (2, 1))

    def test_fully_settled_failure_without_denial_allows_final_gate_without_promoting_member(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                state = self.fixture(denial=False, changed=changed)
                projected = self.inspect(state)
                inspection = projected["result_inspection"]
                self.assertEqual((inspection["status"], inspection["first_blocker"]),
                                 ("INCOMPLETE_EVIDENCE", "COMMAND_FAILURE"))
                self.assertEqual(inspection["post_batch_verifier_settlement"]["policy_denials"], 0)
                self.assertEqual(inspection["post_batch_verifier_settlement"]["failed"], 1)
                self.assertEqual(inspection["commands"][0]["receipt_check"], "matched")
                self.assertEqual(inspection["commands"][0]["execution_proof"], "known_failure")
                self.assertNotIn("failure_resolution", inspection["commands"][0])
                gate, project = self.gate_fixture(projected)
                try:
                    with patch.object(batch, "_project", side_effect=project):
                        result = batch.run_batch(gate.plan, gate.digest)
                    self.assertEqual(result["post_batch_verifier"]["status"], "PASSED")
                    self.assertEqual((result["status"], result["first_typed_blocker"]), ("BLOCKED", "COMMAND_FAILURE"))
                    self.assertEqual(result["members"][0]["terminal"]["result_inspection"], inspection)
                    self.assertEqual(gate.gate_call.call_count, 1)
                finally:
                    gate.doCleanups()

    def test_native_status_and_normalized_reply_allow_settlement_but_not_failure_resolution(self):
        controls = ({}, {"status": "doing", "task_group": "source context delivery", "task_type": ["refactoring"]},
                    {"status": "done", "final_reply": {"verified": True, "parent_acceptance": "pending"}},
                    {"status": "done", "final_reply": "Verified; parent acceptance pending."},
                    {"status": "done", "final_reply": ["Verified", {"parent_acceptance": "pending"}]})
        for denial in (False, True):
            for control in controls:
                with self.subTest(denial=denial, control=control):
                    def mutate(state):
                        state["events"].append(self.evidence.native_task_status_event(control, native=True,
                            normalized_reply=isinstance(control.get("final_reply"), (dict, list))))
                    inspection = self.inspect(self.fixture(mutate, denial=denial))["result_inspection"]
                    self.assertEqual(inspection["post_batch_verifier_settlement"]["policy_denials"], int(denial))
                    self.assertEqual(inspection["first_blocker"], "COMMAND_FAILURE")
                    self.assertEqual(inspection["status"], "INCOMPLETE_EVIDENCE")

    def test_unknown_receipts_and_ambiguous_native_status_never_settle_without_denials(self):
        for case in ("receipt", "outcome", "missing_identity", "duplicate_identity", "foreign_identity",
                     "mixed_output", "forged_reply"):
            with self.subTest(case=case):
                def mutate(state):
                    event = self.evidence.native_task_status_event(
                        {"status": "done", "final_reply": {"verified": True}}, native=True, normalized_reply=True)
                    item = event["item"]
                    if case == "receipt":
                        state["failed"]["aggregated_output"].pop("terminal_receipt")
                    elif case == "outcome":
                        state["failed"]["aggregated_output"]["outcome"] = "unknown"
                    elif case == "missing_identity":
                        item.pop("command_id")
                    elif case == "foreign_identity":
                        item["runtime_id"] = "runtime-foreign"
                    elif case == "duplicate_identity":
                        state["events"].append(deepcopy(event))
                    else:
                        output = json.loads(item["aggregated_output"])
                        if case == "mixed_output":
                            output["results"] = [{"command_type": "shell"}]
                        else:
                            output["task_status"]["final_reply"] = '{"verified":1}'
                        item["aggregated_output"] = json.dumps(output)
                    state["events"].append(event)
                inspection = self.inspect(self.fixture(mutate, denial=False))["result_inspection"]
                self.assertEqual(inspection["first_blocker"], "COMMAND_FAILURE")
                self.assertNotIn("post_batch_verifier_settlement", inspection)

    def test_missing_malformed_unbound_or_duplicate_witness_never_settles(self):
        mutations = [
            ("missing_witness", lambda s: s["events"][0]["item"].pop("policy_denial")),
            ("missing_identity", lambda s: s["events"][0]["item"].pop("command_id")),
            ("wrong_identity", lambda s: s["events"][0]["item"].update(command_id="foreign")),
            ("duplicate_identity", lambda s: s["events"].insert(1, deepcopy(s["events"][0]))),
            ("foreign_scope", lambda s: s["contract"]["read_scopes"].pop()),
            ("disabled_read", lambda s: s["contract"].update(source_read=False)),
            ("effect_claim", lambda s: s["events"][0]["item"].update(changes=["unsettled"])),
            ("unknown_execution", lambda s: s["passed"]["aggregated_output"].pop("terminal_receipt")),
            ("unknown_unindexed", lambda s: s["events"].insert(1, {"type": "item.completed", "item": {
                "type": "command_execution", "command": "unknown", "status": "completed",
                "aggregated_output": {"task_status": {"status": "done", "unknown": True}}}})),
        ]
        for key in denial_event()["item"]["policy_denial"]:
            mutations.append(("missing_" + key, lambda s, key=key: s["events"][0]["item"]["policy_denial"].pop(key)))
        for key, value in (("mutation_count", True), ("mutation_count", 1), ("effect_state", "unknown"),
                           ("authority_effect", "workspace"), ("target", "foreign.py"), ("replayable", 1)):
            mutations.append(("invalid_" + key, lambda s, key=key, value=value:
                s["events"][0]["item"]["policy_denial"].update({key: value})))
        for name, mutate in mutations:
            with self.subTest(case=name):
                state = self.fixture(mutate)
                inspection = self.inspect(state)["result_inspection"]
                self.assertEqual(inspection["artifact_integrity"], "verified")
                self.assertNotIn("post_batch_verifier_settlement", inspection)
                self.assertNotEqual(inspection["status"], "EVIDENCE_VERIFIED")

    def test_integrity_cleanup_and_terminal_uncertainty_never_settle(self):
        for case in ("integrity", "cleanup", "terminal", "exit", "fallback", "contract_drift",
                     "zero_denial_contract_drift"):
            with self.subTest(case=case):
                state = self.fixture(denial=case != "zero_denial_contract_drift", changed=case != "zero_denial_contract_drift")
                terminal, run = state["terminal"], state["run"]
                if case == "integrity":
                    (run / "core.jsonl").write_text("{}\n")
                elif case == "cleanup":
                    terminal["cleanup_pass"] = False
                elif case == "terminal":
                    terminal.update(status="BLOCKED", first_typed_blocker="NOKIY_EMBEDDED_CANCELLED")
                elif case == "exit":
                    terminal["runtime_exit_code"] = 1
                elif case == "fallback":
                    terminal["fallback_used"] = True
                else:
                    evidence_fixtures.store(run / "jspace.json", {})
                evidence_fixtures.store(run / "terminal.json", terminal)
                self.assertNotIn("post_batch_verifier_settlement", self.inspect(state)["result_inspection"])

    def test_cancellation_after_settled_members_still_prevents_final_gate(self):
        for denial in (False, True):
            with self.subTest(denial=denial):
                projected = self.inspect(self.fixture(denial=denial))
                self.assertIn("post_batch_verifier_settlement", projected["result_inspection"])
                gate, project = self.gate_fixture(projected, cancelled=True)
                try:
                    with patch.object(batch, "_project", side_effect=project):
                        result = batch.run_batch(gate.plan, gate.digest)
                    self.assertEqual(result["post_batch_verifier"]["status"], "NOT_RUN")
                    self.assertEqual((result["status"], result["first_typed_blocker"]), ("BLOCKED", "COMMAND_FAILURE"))
                    gate.gate_call.assert_not_called()
                finally:
                    gate.doCleanups()

    def test_blocked_worker_marker_survives_a_passing_independent_gate(self):
        state = self.fixture(lambda s: s["original"].update(terminal_delivery="evidence_only"))
        terminal = state["terminal"]
        marker = {"type": "nokiy.terminal_evidence", "schema_version": "nokiy_terminal_evidence_v1",
            "session_id": "full-" + terminal["request_sha256"], "runtime_id": "runtime-policy-denial",
            "terminal_status": "blocked", "delivery_mode": "evidence_only",
            "parent_acceptance_required": True, "final_summary_turn_executed": False}
        state["events"][-2] = marker
        terminal.update(requested_terminal_delivery="evidence_only", observed_terminal_delivery="evidence_only",
            terminal_evidence=marker, result_text=gate_fixtures.caller._canonical_bytes({key: marker[key] for key in (
                "terminal_status", "delivery_mode", "parent_acceptance_required", "final_summary_turn_executed")}).decode())
        self.evidence.republish_terminal_evidence(terminal, state["run"], state["events"])
        projected = self.inspect(state)
        self.assertIn("post_batch_verifier_settlement", projected["result_inspection"])
        gate, project = self.gate_fixture(projected)
        with patch.object(batch, "_project", side_effect=project):
            result = batch.run_batch(gate.plan, gate.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "PASSED")
        self.assertEqual((result["status"], result["first_typed_blocker"]), ("BLOCKED", "COMMAND_FAILURE"))
        self.assertEqual(result["members"][0]["terminal"]["terminal_evidence"], marker)
        self.assertEqual(gate.gate_call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
