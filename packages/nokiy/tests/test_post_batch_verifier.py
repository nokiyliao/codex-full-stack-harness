# SPDX-License-Identifier: MIT
"""Offline regression coverage plus opt-in real ParentVerifier; no provider sends."""
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_nokiy_batch as fixtures
from codex_collaboration_harness import batch, embedded_nokiy as caller, local_context
from codex_collaboration_harness import post_batch_verifier as post


def readonly_action(reads, command):
    return {"mission": {"mission_id": "post-batch-tests", "task_id": "final",
                "mode": "DELIVERY", "objective": "Verify all member source postimages",
                "current_predicate": "Read-only final verifier"},
            "context_summary": "Disposable exact-scope fixture; no provider sends.",
            "operations": ["read", "command"], "source_read": True,
            "read_scopes": reads, "write_scopes": [], "target_paths": [],
            "command_templates": [], "forbidden_effects": ["create", "modify", "delete", "network"],
            "verifier_commands": [command]}


class PostBatchVerifierTests(unittest.TestCase):
    # Existing offline fixtures allocate exclusively beneath the runner's TMPDIR.
    write_scopes = fixtures.BatchFixture.write_scopes
    terminal = staticmethod(fixtures.BatchFixture.terminal)
    mocks = fixtures.BatchFixture.mocks
    save_plan = fixtures.BatchFixture.save_plan

    def setUp(self):
        fixtures.BatchFixture.setUp(self)
        # Unit workers below are mocks. Native process-root admission is tested
        # separately; it cannot be inherited from a nested verifier test process.
        self.enterContext(patch.object(batch, "native_batch_scope", side_effect=lambda *_: nullcontext()))
        self.workers = tuple(self.requests)
        root = self.plan.parent
        self.outputs = [f"output-{i}.txt" for i in range(len(self.workers))]
        for name in self.outputs:
            (self.workspace / name).write_text("before\n")
        artifacts = root / "final-artifacts"
        artifacts.mkdir()
        scratch = artifacts / "scratch"
        scratch.mkdir()
        executable = root / "python3.13"
        executable.write_bytes(bytes.fromhex("cffaedfe") + b"offline; never spawned\n")
        executable.chmod(0o755)
        request_path = root / "final-request.json"
        request_path.write_text('{"index":2}')
        contract_path = root / "final-jspace.json"
        action = readonly_action(self.outputs, {
                "argv": [str(executable), "-B", "-m", "unittest", "test_combined"],
                "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "pinned_files": [], "timeout_seconds": 7, "network": False,
                "scratch_root": str(scratch), "cwd": str(self.workspace),
                "python_import_roots": [str(Path(__file__).resolve().parent)]})
        self.capsule, contract = local_context.compile_context(self.workspace, action, artifact_root=artifacts)
        local_context.verify_context(self.workspace, self.capsule, contract, artifact_root=artifacts)
        self.final = copy(self.workers[0])
        self.final.request_id = "tura_embedded_" + "f" * 64
        self.final.authority_effect = "none"
        self.final.artifact_root = artifacts
        self.final.jspace_contract = SimpleNamespace(path=contract_path, sha256="")
        identity = SimpleNamespace(path=request_path)
        self.final.context_capsule = self.final.runtime_image = self.final.codex = identity
        for worker in self.workers:
            worker.context_capsule = worker.runtime_image = worker.codex = identity
        self.set_contract(contract)
        self.requests.append(self.final)
        self.spec = {"request_path": str(request_path),
            "request_file_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
            "request_id": self.final.request_id, "artifact_root": str(artifacts),
            "native_thread_id": fixtures.THREAD}
        self.save_post_plan()
        self.pre, self.exe = self.mocks(execute=self.execute)
        self.reader = self.enterContext(patch.object(caller, "read_terminal",
            side_effect=lambda root, rid: self.terminal(next(r for r in self.workers if r.request_id == rid))))
        router = SimpleNamespace(path=executable, verify=Mock())
        self.ready = self.enterContext(patch.object(post.full_core, "prepare",
            side_effect=lambda request, **kwargs: ({"status": "READY"}, SimpleNamespace(artifacts={"tura_router": router}),
                                         {}, deepcopy(self.contract))))
        self.blocker = self.enterContext(patch.object(post.full_core, "verifier_dispatch_blocker", return_value=None))
        self.gate_call = self.enterContext(patch.object(post.ParentVerifier, "handle_request", autospec=True,
                                                       side_effect=self.observation))

    def set_contract(self, contract):
        self.contract = contract
        path = self.final.jspace_contract.path
        path.write_text(json.dumps(contract))
        self.final.jspace_contract.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    def save_post_plan(self):
        self.plan.write_text(json.dumps({"schema_version": batch.SCHEMA, "members": self.members,
                                        "post_batch_verifier": self.spec}))
        self.digest = hashlib.sha256(self.plan.read_bytes()).hexdigest()

    def execute(self, request):
        # These effects stand in for already-admitted workers, never the verifier.
        index = self.workers.index(request)
        (self.workspace / self.outputs[index]).write_text(f"after member {index}\n")
        run = request.artifact_root / request.request_id
        run.mkdir()
        terminal = self.terminal(request)
        caller._write_create_only(run / "terminal.json", terminal)
        return terminal

    def parent_gate(self):
        self.parent_source = self.workspace / "parent.txt"
        self.parent_source.write_text("parent completed\n")
        action = readonly_action(self.outputs + ["parent.txt"], self.contract["verifier_commands"][0])
        self.capsule, contract = local_context.compile_context(self.workspace, action,
                                                              artifact_root=self.final.artifact_root)
        self.set_contract(contract)
        self.spec["parent_source_targets"] = ["parent.txt"]
        self.save_post_plan()
        members, _ = batch._load_plan(self.plan, self.digest)
        gate = post.prepare(self.spec, self.plan, self.digest, members, self.workers,
                            [batch._scopes(r) for r in self.workers])
        self.assertTrue(post.claim(gate, self.plan))
        return gate

    def test_parent_completion_waits_for_explicit_original_bound_ack(self):
        gate = self.parent_gate()
        cancelled = threading.Event()
        entered = threading.Event()
        sleep = post.time.sleep
        def observed_sleep(seconds):
            entered.set()
            sleep(seconds)
        with patch.object(post.time, "sleep", side_effect=observed_sleep), ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(post._wait_for_parent, gate, cancelled)
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(waiting.done())
                outcome = post.acknowledge_parent(gate)
                self.assertFalse(outcome["verifier_executed"])
                self.assertTrue(waiting.result(timeout=1))
            finally:
                cancelled.set()
        self.gate_call.assert_not_called()
        with self.assertRaises((FileExistsError, caller.EmbeddedNokiyError)):
            post.acknowledge_parent(gate)

    def test_parent_completion_cancel_and_wrong_owner_do_not_seal(self):
        gate = self.parent_gate()
        cancelled = threading.Event()
        cancelled.set()
        self.assertFalse(post._wait_for_parent(gate, cancelled))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign"}):
            with self.assertRaises(caller.EmbeddedNokiyError):
                post.acknowledge_parent(gate)
        self.assertFalse((post._run(gate.spec) / "parent-complete.json").exists())
        self.gate_call.assert_not_called()

    def test_parent_completion_reconstructs_original_plan_without_dispatch(self):
        gate = self.parent_gate()
        result = post.acknowledge_parent_plan(self.plan, self.digest)
        self.assertEqual(result["status"], "PARENT_SNAPSHOT_SEALED")
        self.assertTrue(post._check_parent_completion(gate))
        self.exe.assert_not_called()
        self.gate_call.assert_not_called()

    def test_parent_completion_cli_uses_original_plan_without_worker_or_verifier(self):
        gate = self.parent_gate()
        with patch.object(caller, "_emit_json") as emit:
            code = caller.main(["complete-batch-parent", "--plan", str(self.plan),
                                "--plan-sha256", self.digest])
        self.assertEqual(code, 0)
        self.assertEqual(emit.call_args.args[0]["status"], "PARENT_SNAPSHOT_SEALED")
        self.assertTrue(post._check_parent_completion(gate))
        self.exe.assert_not_called()
        self.gate_call.assert_not_called()

    def test_parent_completion_wrong_plan_digest_does_not_seal(self):
        gate = self.parent_gate()
        with self.assertRaises(caller.EmbeddedNokiyError):
            post.acknowledge_parent_plan(self.plan, "0" * 64)
        self.assertFalse((post._run(gate.spec) / "parent-complete.json").exists())

    def test_parent_completion_rejects_changed_source_and_binding(self):
        gate = self.parent_gate()
        post.acknowledge_parent(gate)
        self.parent_source.write_text("changed after completion\n")
        with self.assertRaises(caller.EmbeddedNokiyError):
            post._wait_for_parent(gate, threading.Event())
        self.gate_call.assert_not_called()

    def test_parent_completion_ignores_metadata_only_changes_outside_gate(self):
        gate = self.parent_gate()
        post.acknowledge_parent(gate)
        self.parent_source.touch()
        (self.workspace / "independent-output.txt").write_text("other branch")
        self.assertTrue(post._check_parent_completion(gate))
        self.parent_source.chmod((self.parent_source.stat().st_mode & 0o777) ^ 0o100)
        with self.assertRaises(caller.EmbeddedNokiyError):
            post._check_parent_completion(gate)

    def test_parent_subjects_cannot_overlap_worker_writes_or_expand_reads(self):
        gate = self.parent_gate()
        members, _ = batch._load_plan(self.plan, self.digest)
        for names in ([self.outputs[0]], ["outside.txt"]):
            spec = {**gate.spec, "parent_source_targets": names}
            with self.subTest(names=names), self.assertRaises(caller.EmbeddedNokiyError):
                post.prepare(spec, self.plan, self.digest, members, self.workers,
                             [batch._scopes(r) for r in self.workers])

    def test_parent_completion_full_finish_and_deleted_ack_fail_closed(self):
        gate = self.parent_gate()
        for request in self.workers:
            self.exe(request)
        post.acknowledge_parent(gate)
        observe = self.observation
        def delete_ack(*args):
            result = observe(*args)
            (post._run(gate.spec) / "parent-complete.json").unlink()
            return result
        self.gate_call.side_effect = delete_ack
        result = post.finish(gate, {"status": "RESULT_AVAILABLE"})
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")
        self.assertEqual(self.gate_call.call_count, 1)

    def test_parent_completion_full_finish_accepts_stable_parent_and_workers(self):
        gate = self.parent_gate()
        for request in self.workers:
            self.exe(request)
        post.acknowledge_parent(gate)
        result = post.finish(gate, {"status": "RESULT_AVAILABLE"})
        self.assertEqual(result["post_batch_verifier"]["status"], "PASSED", result)
        self.assertIn("parent.txt", result["post_batch_verifier"]["source_postimages"])
        self.assertEqual(self.gate_call.call_count, 1)

    def observation(self, runner, index, call_id, cancel):
        self.assertEqual(index, 0)
        self.assertEqual(call_id, "post-batch-" + self.digest)
        self.assertFalse(cancel.is_set())
        self.assertEqual(self.exe.call_count, len(self.workers))
        self.assertTrue(all((r.artifact_root / r.request_id / "terminal.json").is_file() for r in self.workers))
        self.assertEqual(runner.contract["write_scopes"], [])
        self.assertEqual(runner.contract["declared_targets"], [])
        self.assertEqual(runner._verification_source_targets,
                         sorted(self.outputs + self.spec.get("parent_source_targets", [])))
        sources = post._capture_verification_sources(runner.workspace, runner.contract,
                                                     source_targets=runner._verification_source_targets)
        return {"success": True, "exit_code": 0, "stdout": "raw stdout\n", "stderr": "raw stderr\n",
                "process_reaped": True, "process_group_empty": True, "outcome": "known",
                "verification_evidence": {"schema_version": post.VERIFICATION_EVIDENCE_SCHEMA,
                    "authorization_semantic_sha256": runner.binding, "verifier_index": 0,
                    "verifier_sha256": caller._canonical_sha256(self.contract["verifier_commands"][0]),
                    "call_id": call_id, "source_postimages": {name: row[0] for name, row in sources.items()}}}

    def settled_terminal(self, request, *, failed=True, gap=False):
        terminal = fixtures.BatchFixture.terminal(request)
        terminal.update(fallback_used=False, runtime_exit_code=0,
                        request_sha256=request.request_id.removeprefix("tura_embedded_"))
        if failed:
            terminal["result_inspection"] = {
                "status": "INCOMPLETE_EVIDENCE", "first_blocker": "COMMAND_FAILURE",
                "artifact_integrity": "verified", "command_success": "known_failure",
                "terminal_sha256": "a" * 64,
                "counts": {"commands": 2, "failed": 1, "index_complete": True},
                "post_batch_verifier_settlement": {
                    "schema_version": "nokiy_post_batch_verifier_settlement_v2" if gap
                                      else "nokiy_post_batch_verifier_settlement_v1",
                    "terminal_sha256": "a" * 64, "commands": 2, "failed": 1,
                    "policy_denials": 0, "unsettled_commands": 0}}
        else:
            terminal["result_inspection"] = {"status": "EVIDENCE_VERIFIED", "first_blocker": None,
                                             "artifact_integrity": "verified"}
        if gap:
            terminal["capability_gap"] = {
                "schema_version": "nokiy_capability_gap_v1", "status": "MODEL_REPORTED",
                "first_blocker": None, "trust": "model_reported_diagnostic",
                "requires_parent_readmission": True, "permission_grant": False,
                "authority_effect": "none", "execution_proof": "unproven", "retry_safe": False,
                "mission_acceptance": "parent_owned",
                "binding": {key: terminal[key] for key in ("request_id", "request_sha256", "native_thread_id")},
                "handoff": {"schema_version": "nokiy_capability_gap_v1",
                    "missing_capabilities": [{"path": self.outputs[1], "operation": "read", "tool": "source_read"}],
                    "completed_work": ["Candidate written; focused verifier failed."],
                    "remaining_work": ["Run the already-admitted combined verifier after the peer settles."],
                    "unified_diff": None}}
            terminal["capability_gap"]["binding"].update(
                terminal_sha256="a" * 64, terminal_content_sha256="b" * 64)
        return terminal

    def test_settled_failure_runs_final_gate_without_erasing_member_failure(self):
        self.terminal = self.settled_terminal
        with patch.object(batch, "_project", side_effect=lambda member, terminal: terminal):
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["first_typed_blocker"], "COMMAND_FAILURE")
        self.assertEqual(self.gate_call.call_count, 1)
        self.assertEqual(result["post_batch_verifier"]["status"], "PASSED")
        for worker in self.workers:
            terminal = json.loads((worker.artifact_root / worker.request_id / "terminal.json").read_text())
            inspection = terminal["result_inspection"]
            self.assertEqual(inspection["command_success"], "known_failure")
            self.assertEqual(inspection["counts"]["failed"], 1)

    def test_settled_verifier_gap_runs_once_without_promoting_member_or_batch(self):
        self.terminal = lambda request: self.settled_terminal(
            request, failed=request == self.workers[0], gap=request == self.workers[0])
        with patch.object(batch, "_project", side_effect=lambda member, terminal: terminal):
            result = batch.run_batch(self.plan, self.digest)
            self.assertEqual(result["post_batch_verifier"]["status"], "PASSED")
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["first_typed_blocker"], "COMMAND_FAILURE")
            self.assertEqual(result["mission_acceptance"], "parent_owned")
            member = result["members"][0]["terminal"]
            saved = json.loads((self.workers[0].artifact_root / self.workers[0].request_id / "terminal.json").read_text())
            self.assertEqual(member["capability_gap"], saved["capability_gap"])
            self.assertEqual(member["result_inspection"], saved["result_inspection"])
            self.assertEqual(member["result_inspection"]["command_success"], "known_failure")
            self.assertEqual(self.gate_call.call_count, 1)
            self.assert_unknown_without_replay()

    def assert_diagnostic_gap_blocks_gate(self, mutate):
        def terminal(request):
            value = self.settled_terminal(request, gap=True)
            mutate(value)
            return value
        self.terminal = terminal
        with patch.object(batch, "_project", side_effect=lambda member, value: value):
            self.assert_member_blocks_gate(self.execute)

    def test_legacy_settlement_cannot_admit_a_diagnostic_gap(self):
        self.assert_diagnostic_gap_blocks_gate(lambda terminal: terminal["result_inspection"][
            "post_batch_verifier_settlement"].update(schema_version="nokiy_post_batch_verifier_settlement_v1"))

    def test_diagnostic_gap_cannot_admit_policy_denial(self):
        self.assert_diagnostic_gap_blocks_gate(lambda terminal: terminal["result_inspection"][
            "post_batch_verifier_settlement"].update(policy_denials=1))

    def test_diagnostic_gap_binding_must_match_original_member(self):
        self.assert_diagnostic_gap_blocks_gate(lambda terminal: terminal["capability_gap"]["binding"].update(
            request_id="tura_embedded_" + "f" * 64))

    def test_diagnostic_gap_unavailable_is_not_a_model_report(self):
        self.assert_diagnostic_gap_blocks_gate(lambda terminal: terminal["capability_gap"].update(
            status="UNAVAILABLE", first_blocker="CAPABILITY_GAP_SCHEMA_INVALID"))

    def test_diagnostic_gap_cannot_grant_permission(self):
        self.assert_diagnostic_gap_blocks_gate(lambda terminal: terminal["capability_gap"].update(permission_grant=True))

    def test_diagnostic_gap_attestation_must_match_inspected_terminal(self):
        self.assert_diagnostic_gap_blocks_gate(lambda terminal: terminal["result_inspection"][
            "post_batch_verifier_settlement"].update(terminal_sha256="c" * 64))

    def test_diagnostic_gap_cannot_admit_unsettled_execution(self):
        self.assert_diagnostic_gap_blocks_gate(lambda terminal: terminal["result_inspection"][
            "post_batch_verifier_settlement"].update(unsettled_commands=1))

    def test_diagnostic_gap_cannot_admit_failed_cleanup(self):
        self.assert_diagnostic_gap_blocks_gate(lambda terminal: terminal.update(cleanup_pass=False))

    def test_join_once_receipt_postimages_and_read_only_recovery(self):
        settled = set()
        execute = self.execute
        def worker(request):
            terminal = execute(request)
            settled.add(request.request_id)
            return terminal
        self.exe.side_effect = worker
        observe = self.observation
        def gate(*args):
            self.assertEqual(settled, {r.request_id for r in self.workers})
            return observe(*args)
        self.gate_call.side_effect = gate
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        projected = result["post_batch_verifier"]
        self.assertEqual(projected["status"], "PASSED")
        receipt = json.loads(Path(projected["receipt_path"]).read_text())
        self.assertEqual(receipt["binding"]["plan_sha256"], self.digest)
        self.assertEqual(receipt["binding"]["request"]["native_thread_id"], fixtures.THREAD)
        self.assertEqual(receipt["binding"]["command"], self.contract["verifier_commands"][0])
        self.assertEqual(receipt["observation"]["stdout"], "raw stdout\n")
        self.assertEqual(receipt["observation"]["stderr"], "raw stderr\n")
        self.assertEqual(set(receipt["observation"]["verification_evidence"]["source_postimages"]), set(self.outputs))
        self.assertEqual(projected["binding"], receipt["binding"])
        self.assertEqual(projected["binding"]["source_targets"], self.outputs)
        self.assertEqual(projected["source_postimages"],
                         receipt["observation"]["verification_evidence"]["source_postimages"])
        self.assertEqual(projected["evidence_authority"], "live_return")
        self.assertEqual(projected["output_preview"], {"stdout": "raw stdout\n", "stderr": "raw stderr\n"})
        self.assertEqual(projected["output_preview_authority"],
                         "untrusted_verifier_output_not_parent_acceptance")
        self.assertEqual(hashlib.sha256(Path(projected["receipt_path"]).read_bytes()).hexdigest(), projected["receipt_sha256"])
        for recover in (batch.read_batch, batch.run_batch):
            recovered = recover(self.plan, self.digest)
            self.assertEqual(recovered["post_batch_verifier"]["status"], "UNKNOWN")
            self.assertEqual(recovered["status"], "BLOCKED")
            self.assertEqual(recovered["post_batch_verifier"]["receipt_sha256"], projected["receipt_sha256"])
            self.assertFalse(recovered["post_batch_verifier"]["auto_retry_allowed"])
            self.assertEqual(recovered["post_batch_verifier"]["evidence_authority"], "diagnostic_only")
            self.assertNotIn("output_preview", recovered["post_batch_verifier"])
            summary = batch.summarize(recovered)
            self.assertEqual(summary["post_batch_verifier"], recovered["post_batch_verifier"])
            self.assertEqual(summary["mission_acceptance"], "parent_owned")
            self.assertLessEqual(len(json.dumps(summary).encode()), batch.MAX_PROJECTED_BYTES)
        self.assertEqual((self.exe.call_count, self.gate_call.call_count, self.ready.call_count), (2, 1, 1))

    def test_live_gate_lists_files_left_at_its_scratch_root(self):
        scratch = Path(self.contract["verifier_commands"][0]["scratch_root"])
        report = b'{"status":"PASS"}\n'
        observation = self.observation
        def gate(*args):
            (scratch / "quality.json").write_bytes(report)
            (scratch / ".hidden").write_text("skip\n")
            (scratch / "nested").mkdir()
            os.symlink(scratch / "quality.json", scratch / "alias.json")
            return observation(*args)
        self.gate_call.side_effect = gate
        projected = batch.run_batch(self.plan, self.digest)["post_batch_verifier"]
        self.assertEqual(projected["status"], "PASSED")
        self.assertEqual(projected["verifier_outputs"], {"files": [{"path": str(scratch / "quality.json"),
            "bytes": len(report), "sha256": hashlib.sha256(report).hexdigest()}], "truncated": False})
        self.assertEqual(projected["verifier_outputs_authority"],
                         "untrusted_verifier_written_files_not_parent_acceptance")
        recovered = batch.read_batch(self.plan, self.digest)["post_batch_verifier"]
        self.assertEqual(recovered["status"], "UNKNOWN")
        self.assertNotIn("verifier_outputs", recovered)

    def test_large_verifier_output_keeps_digest_without_partial_preview(self):
        observation = self.observation
        def large(*args):
            value = observation(*args)
            value["stdout"] = "x" * (post.INLINE_EVIDENCE_BYTES + 1)
            return value
        self.gate_call.side_effect = large
        gate = batch.run_batch(self.plan, self.digest)["post_batch_verifier"]
        self.assertEqual(gate["status"], "PASSED")
        self.assertNotIn("output_preview", gate)
        self.assertEqual(gate["output_preview_omission"], "INLINE_EVIDENCE_BYTE_LIMIT")
        self.assertEqual(gate["stdout_bytes"], post.INLINE_EVIDENCE_BYTES + 1)
        self.assertEqual(gate["stdout_sha256"], hashlib.sha256(
            ("x" * (post.INLINE_EVIDENCE_BYTES + 1)).encode()).hexdigest())

    def test_missing_field_keeps_old_result_and_summary(self):
        self.save_plan()
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        self.assertNotIn("post_batch_verifier", result)
        self.assertNotIn("post_batch_verifier", batch.summarize(result))
        self.ready.assert_not_called()
        self.gate_call.assert_not_called()

    def test_invalid_declaration_or_owner_rejected_before_preflight(self):
        original = deepcopy(self.spec)
        for changes in ({"unknown": True}, {"native_thread_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
                        {"request_path": str(self.plan.parent / ".." / "request.json")},
                        {"artifact_root": self.members[0]["artifact_root"]}):
            self.spec = dict(original, **changes)
            self.save_post_plan()
            with self.assertRaises((caller.EmbeddedNokiyError, OSError)):
                batch.run_batch(self.plan, self.digest)
        self.pre.assert_not_called()
        self.exe.assert_not_called()
        self.gate_call.assert_not_called()

    def test_unsafe_typed_grant_rejected_before_workers(self):
        original = deepcopy(self.contract)
        for changes in ({"network": True}, {"unknown": True},
                        {"argv": [original["verifier_commands"][0]["argv"][0], "-c", "inline"]},
                        {"scratch_root": str(self.workspace)}):
            contract = deepcopy(original)
            contract["verifier_commands"][0].update(changes)
            self.set_contract(contract)
            with self.assertRaises(caller.EmbeddedNokiyError):
                batch.run_batch(self.plan, self.digest)
        self.exe.assert_not_called()
        self.gate_call.assert_not_called()
        self.assertFalse(post.exists(self.spec))

    def test_unavailable_native_fence_is_no_effects(self):
        self.blocker.return_value = "NOKIY_FULL_CORE_VERIFIER_OUTER_SCOPE_REQUIRED"
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "OUTER_SCOPE_REQUIRED"):
            batch.run_batch(self.plan, self.digest)
        self.exe.assert_not_called()
        self.assertFalse(post.exists(self.spec))

    def test_missing_combined_source_binding_rejected(self):
        contract = deepcopy(self.contract)
        contract["read_scopes"] = self.outputs[:1]
        self.set_contract(contract)
        with self.assertRaises(caller.EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        self.exe.assert_not_called()

    def test_writable_final_rejected_before_admission(self):
        contract = deepcopy(self.contract)
        contract.update(write_scopes=self.outputs, declared_targets=self.outputs,
                        allowed_operations=["read", "command", "modify"])
        self.set_contract(contract)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "read/command-only"):
            batch.run_batch(self.plan, self.digest)
        self.ready.assert_not_called()
        self.exe.assert_not_called()

    def test_workspace_effect_final_rejected_before_admission(self):
        self.final.authority_effect = "workspace"
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "read/command-only"):
            batch.run_batch(self.plan, self.digest)
        self.ready.assert_not_called()
        self.exe.assert_not_called()

    def test_raw_binding_and_postimages_are_bounded_without_truncation(self):
        with patch.object(post, "INLINE_EVIDENCE_BYTES", 1):
            result = batch.run_batch(self.plan, self.digest)
        gate = result["post_batch_verifier"]
        self.assertEqual(gate["status"], "PASSED")
        self.assertEqual(gate["evidence_omission"], "INLINE_EVIDENCE_BYTE_LIMIT")
        self.assertNotIn("binding", gate)
        self.assertNotIn("source_postimages", gate)
        receipt = json.loads(Path(gate["receipt_path"]).read_text())
        raw = caller._canonical_bytes({"binding": receipt["binding"],
            "source_postimages": receipt["observation"]["verification_evidence"]["source_postimages"]})
        self.assertEqual(gate["evidence_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(gate["evidence_bytes"], len(raw))

    def test_empty_proof_from_legacy_write_target_selection_stays_unknown(self):
        observe = self.observation
        def empty(*args):
            result = observe(*args)
            result["verification_evidence"]["source_postimages"] = {}
            return result
        self.gate_call.side_effect = empty
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")

    def test_source_mutation_after_verifier_return_stays_unknown(self):
        observe = self.observation
        def changed(*args):
            result = observe(*args)
            (self.workspace / self.outputs[0]).write_text("changed after observation")
            return result
        self.gate_call.side_effect = changed
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")
        self.assert_unknown_without_replay()

    def test_source_alias_after_verifier_return_stays_unknown(self):
        observe = self.observation
        def aliased(*args):
            result = observe(*args)
            source = self.workspace / self.outputs[0]
            replacement = self.workspace / "replacement.txt"
            replacement.write_bytes(source.read_bytes())
            source.unlink()
            source.symlink_to(replacement)
            return result
        self.gate_call.side_effect = aliased
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")
        self.assert_unknown_without_replay()

    def test_changed_claim_subjects_never_dispatch(self):
        gate = post.prepare(self.spec, self.plan, self.digest, (), self.workers,
                            [batch._scopes(r) for r in self.workers])
        self.assertTrue(post.claim(gate, self.plan))
        path = post._run(self.spec) / "claim.json"
        claim = json.loads(path.read_text())
        claim["source_targets"] = self.outputs[:1]
        path.write_text(json.dumps(claim))
        self.assertEqual(post.finish(gate, {"status": "RESULT_AVAILABLE"})[
            "post_batch_verifier"]["status"], "UNKNOWN")
        self.gate_call.assert_not_called()

    def assert_member_blocks_gate(self, execute):
        self.exe.side_effect = execute
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["post_batch_verifier"]["status"], "NOT_RUN")
        self.assertEqual(self.exe.call_count, 2)
        self.gate_call.assert_not_called()

    def test_failed_member_blocks_gate(self):
        self.assert_member_blocks_gate(lambda r: self.terminal(r, status="FAILED"))

    def test_unknown_member_blocks_gate(self):
        def uncertain(request):
            raise RuntimeError("interrupted worker")
        self.assert_member_blocks_gate(uncertain)

    def test_rejected_terminal_evidence_blocks_gate(self):
        with patch.object(batch, "_project", side_effect=lambda m, t: {
                **t, "result_inspection": {"status": "EVIDENCE_REJECTED"}}):
            self.assert_member_blocks_gate(self.execute)

    def test_cancellation_before_gate_is_not_a_pass(self):
        execute = self.execute
        def cancelled(request):
            caller._active_cancellation.get().append(15)
            return execute(request)
        self.exe.side_effect = cancelled
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "NOT_RUN")
        self.assertEqual(result["status"], "BLOCKED")
        self.gate_call.assert_not_called()

    def test_failure_and_unknown_never_replay(self):
        self.gate_call.side_effect = None
        self.gate_call.return_value = {"success": False, "exit_code": 1,
            "stdout": "failure output", "stderr": "failed assertion", "outcome": "known",
            "process_reaped": True, "process_group_empty": True}
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["post_batch_verifier"]["status"], "FAILED")
        self.assertEqual(batch.run_batch(self.plan, self.digest)["post_batch_verifier"]["status"], "UNKNOWN")
        self.gate_call.assert_called_once()

    def test_interrupted_gate_remains_unknown(self):
        self.gate_call.side_effect = RuntimeError("lost supervisor observation")
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")
        for recover in (batch.read_batch, batch.run_batch):
            self.assertEqual(recover(self.plan, self.digest)["post_batch_verifier"]["status"], "UNKNOWN")
        self.gate_call.assert_called_once()
        self.assertEqual(self.exe.call_count, 2)

    def test_claim_without_members_is_unknown_not_replayed(self):
        members = batch.load_plan(self.plan, self.digest)
        gate = post.prepare(self.spec, self.plan, self.digest, members, self.workers,
                            [batch._scopes(r) for r in self.workers])
        self.assertTrue(post.claim(gate, self.plan))
        self.assertEqual(batch.run_batch(self.plan, self.digest)["post_batch_verifier"]["status"], "UNKNOWN")
        self.exe.assert_not_called()
        self.gate_call.assert_not_called()

    def test_simultaneous_claims_have_one_owner_and_never_replay(self):
        members = batch.load_plan(self.plan, self.digest)
        gate = post.prepare(self.spec, self.plan, self.digest, members, self.workers,
                            [batch._scopes(r) for r in self.workers])
        barrier = threading.Barrier(2)
        def claim():
            barrier.wait(timeout=5)
            return post.claim(gate, self.plan)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = [pool.submit(claim) for _ in range(2)]
            self.assertEqual(sorted(f.result() for f in claims), [False, True])
        for recover in (batch.read_batch, batch.run_batch):
            self.assertEqual(recover(self.plan, self.digest)["post_batch_verifier"]["status"], "UNKNOWN")
        self.exe.assert_not_called()
        self.gate_call.assert_not_called()

    def test_interrupted_durable_publication_is_unknown_without_replay(self):
        sync = post._sync
        def interrupted(path):
            if (path / "receipt.json").exists():
                raise OSError("receipt directory fsync interrupted")
            return sync(path)
        with patch.object(post, "_sync", side_effect=interrupted):
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")
        self.assertTrue(Path(result["post_batch_verifier"]["receipt_path"]).is_file())
        self.assert_unknown_without_replay()

    def test_publication_must_match_original_live_outcome(self):
        write = caller._write_create_only
        def rewritten(path, value, **kwargs):
            write(path, value, **kwargs)
            if path.name == "receipt.json":
                changed = deepcopy(value)
                changed["observation"]["stdout"] = "rewritten output"
                path.write_bytes(caller._canonical_bytes(changed) + b"\n")
        with patch.object(caller, "_write_create_only", side_effect=rewritten):
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")
        self.assert_unknown_without_replay()

    def test_green_without_postimage_proof_stays_unknown(self):
        observe = self.observation
        def unbound(*args):
            result = observe(*args)
            result.pop("verification_evidence")
            return result
        self.gate_call.side_effect = unbound
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")
        self.assertEqual(result["status"], "BLOCKED")

    def test_receipt_tampering_is_unknown(self):
        result = batch.run_batch(self.plan, self.digest)
        path = Path(result["post_batch_verifier"]["receipt_path"])
        receipt = json.loads(path.read_text())
        receipt["binding"]["command"]["argv"].append("tampered")
        path.write_text(json.dumps(receipt))
        self.assertEqual(batch.read_batch(self.plan, self.digest)["post_batch_verifier"]["status"], "UNKNOWN")
        self.gate_call.assert_called_once()

    def test_later_source_change_invalidates_green_recovery(self):
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "PASSED")
        (self.workspace / self.outputs[0]).write_text("later edit\n")
        self.assertEqual(batch.run_batch(self.plan, self.digest)["post_batch_verifier"]["status"], "UNKNOWN")
        self.gate_call.assert_called_once()

    def assert_unknown_without_replay(self):
        for recover in (batch.read_batch, batch.run_batch):
            recovered = recover(self.plan, self.digest)
            self.assertEqual(recovered["post_batch_verifier"]["status"], "UNKNOWN")
            self.assertEqual(recovered["status"], "BLOCKED")
        self.assertEqual(self.exe.call_count, 2)
        self.gate_call.assert_called_once()

    def test_consistently_rewritten_source_receipt_is_unknown(self):
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "PASSED")
        path = Path(result["post_batch_verifier"]["receipt_path"])
        receipt = json.loads(path.read_text())
        original = deepcopy(receipt)
        target = self.workspace / self.outputs[0]
        target.write_text("unverified replacement\n")
        target.chmod(0o600)
        sources = post._capture_verification_sources(self.workspace, self.contract, source_targets=self.outputs)
        receipt["observation"]["verification_evidence"]["source_postimages"] = {
            name: row[0] for name, row in sources.items()}
        self.assertEqual(receipt["binding"], original["binding"])
        self.assertEqual(receipt["status"], original["status"])
        path.write_text(json.dumps(receipt))
        self.assert_unknown_without_replay()

    def test_source_substitution_with_identical_bytes_is_unknown(self):
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "PASSED")
        target = self.workspace / self.outputs[0]
        replacement = target.with_suffix(".replacement")
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(target.stat().st_mode & 0o777)
        self.assertNotEqual(replacement.stat().st_ino, target.stat().st_ino)
        replacement.replace(target)
        self.assert_unknown_without_replay()

    def test_failed_observation_rewritten_as_success_is_unknown(self):
        observe = self.observation
        def failed(*args):
            self.plausible_success = observe(*args)
            return {**self.plausible_success, "success": False, "exit_code": 1}
        self.gate_call.side_effect = failed
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "FAILED")
        path = Path(result["post_batch_verifier"]["receipt_path"])
        receipt = json.loads(path.read_text())
        receipt.update(status="PASSED", observation=self.plausible_success)
        path.write_text(json.dumps(receipt))
        self.assert_unknown_without_replay()

    def test_interrupted_observation_rewritten_as_success_is_unknown(self):
        observe = self.observation
        def interrupted(*args):
            self.plausible_success = observe(*args)
            raise RuntimeError("lost supervisor observation")
        self.gate_call.side_effect = interrupted
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")
        path = Path(result["post_batch_verifier"]["receipt_path"])
        caller._write_create_only(path, {"binding": json.loads(
            (path.parent / "claim.json").read_text()), "status": "PASSED",
            "observation": self.plausible_success})
        self.assert_unknown_without_replay()


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("NOKIY_VERIFIER_TEST_ROUTER"),
                     "parent-owned: requires exact candidate router and real macOS Seatbelt")
class PostBatchVerifierNativeTests(unittest.TestCase):
    """Run by the unfenced parent with its existing router; no mocked outcomes.

    Entry: python -B -m unittest test_post_batch_verifier.PostBatchVerifierNativeTests
    Use existing PYTHONPATH=src:tests and NOKIY_VERIFIER_TEST_ROUTER settings.
    This exercises real compiler/admission/claim/finish/recovery with the exact
    router. Synthetic workers below have no runtime or provider dispatch.
    """

    def test_real_readonly_verifier_records_absent_directory_subject(self):
        import test_verifier_parent

        f = test_verifier_parent.VerifierParentTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        (f.workspace / "sources").mkdir()
        f.script.write_text(
            "from pathlib import Path\n"
            "p = Path('sources/optional.txt')\n"
            "assert not p.exists()\n"
            "try: p.write_text('unauthorized')\n"
            "except PermissionError: pass\n"
            "else: raise AssertionError('readonly verifier acquired create permission')\n")
        action = readonly_action(["sources/**", "verify.py"], {
            "argv": [str(f.python), str(f.script)], "executable_sha256": caller._file_sha256(f.python),
            "pinned_files": [{"path": str(f.script), "sha256": caller._file_sha256(f.script)}],
            "timeout_seconds": 7, "scratch_root": str(f.scratch), "network": False,
            "cwd": str(f.workspace)})
        action["read_directories"] = ["sources"]
        _, contract = local_context.compile_context(f.workspace, action, artifact_root=f.artifacts)
        runner = post.ParentVerifier(f.workspace, contract, f.router, None,
                                     verification_source_targets=["sources/optional.txt"])
        result = runner.handle_request(0, "readonly-absence", threading.Event())
        self.assertTrue(result["success"], result)
        self.assertEqual(result["verification_evidence"]["source_postimages"],
                         {"sources/optional.txt": None})
        self.assertTrue(runner.cleanup_pass)

    def test_real_parent_verifier_pass_and_forged_recovery_without_replay(self):
        import test_full_core
        import test_verifier_parent

        f = test_verifier_parent.VerifierParentTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        marker = f.scratch / "gate-executions.txt"
        other = f.workspace / "other.txt"
        other.write_text("before")
        parent_source = f.workspace / "parent.txt"
        parent_source.write_text("before parent")
        (f.workspace / "sources").mkdir()
        f.script.write_text(
            "from pathlib import Path\n"
            "assert Path('solution.txt').read_text() == 'after member 0'\n"
            "assert Path('other.txt').read_text() == 'after member 1'\n"
            "assert not Path('sources/optional.txt').exists()\n"
            "assert Path('parent.txt').read_text() == 'after parent'\n"
            "try: Path('solution.txt').write_text('unauthorized')\n"
            "except PermissionError: pass\n"
            "else: raise AssertionError('source write escaped native fence')\n"
            f"with Path({str(marker)!r}).open('a') as stream: stream.write('verified\\n')\n"
            "print('authentic read-only parent verifier')\n")
        action = readonly_action(["solution.txt", "other.txt", "verify.py", "sources/**", "parent.txt"], {
            "argv": [str(f.python), str(f.script)], "executable_sha256": caller._file_sha256(f.python),
            "pinned_files": [{"path": str(f.script), "sha256": caller._file_sha256(f.script)}],
            "timeout_seconds": 7, "scratch_root": str(f.scratch), "network": False, "cwd": str(f.workspace)})
        action["read_directories"] = ["sources"]
        capsule, contract = local_context.compile_context(f.workspace, action, artifact_root=f.artifacts)
        self.assertEqual(contract["write_scopes"], [])
        self.assertEqual(contract["declared_targets"], [])
        self.assertEqual(set(contract["allowed_operations"]), {"read", "command"})
        # Full caller admission uses a complete fixture inventory and the pinned
        # native router. Its synthetic provider/runtime executables are not run.
        prepared = test_full_core.FullCoreTests()
        prepared.setUp()
        self.addCleanup(prepared.doCleanups)
        shutil.copy2(f.router.path, prepared.f.runtime / "tura_router")
        prepared._image()
        contract_path = f.root / "gate-jspace.json"
        capsule_path = f.root / "gate-capsule.json"
        caller._write_create_only(contract_path, contract)
        caller._write_create_only(capsule_path, capsule)
        request = replace(prepared._request(), workspace=f.workspace,
            artifact_root=f.artifacts, execution_profile="direct", authority_effect="none",
            context_capsule=caller.FileIdentity(capsule_path, caller._file_sha256(capsule_path)),
            jspace_contract=caller.FileIdentity(contract_path, caller._file_sha256(contract_path)))
        request_path = f.root / "gate-request.json"
        caller._write_create_only(request_path, request.to_wire(include_identity=False))
        request = caller.load_request(request_path)
        spec = {"request_path": str(request_path), "request_file_sha256": caller._file_sha256(request_path),
                "request_id": request.request_id, "artifact_root": str(request.artifact_root),
                "native_thread_id": request.native_thread_id, "parent_source_targets": ["parent.txt"]}
        plan = f.root / "gate-plan.json"
        caller._write_create_only(plan, {"post_batch_verifier": spec})
        digest = caller._file_sha256(plan)
        workers = []
        for index, name in enumerate(("solution.txt", "other.txt", "sources/optional.txt")):
            artifacts = f.root / f"member-{index}"
            artifacts.mkdir()
            worker_action = readonly_action([name], action["verifier_commands"][0])
            worker_action.pop("verifier_commands")
            worker_action.update(operations=["read", "command", "modify"],
                                 write_scopes=[name], target_paths=[name], forbidden_effects=["network", "delete"])
            if name == "sources/optional.txt":
                worker_action["operations"].append("create")
            _, worker_contract = local_context.compile_context(f.workspace, worker_action)
            worker_path = f.root / f"member-{index}.json"
            caller._write_create_only(worker_path, worker_contract)
            workers.append(replace(request, artifact_root=artifacts,
                jspace_contract=caller.FileIdentity(worker_path, caller._file_sha256(worker_path))))
        with patch.object(caller, "execute", side_effect=AssertionError("provider path reached")):
            ready, _, _, admitted = post.full_core.prepare(request)
            self.assertEqual(ready["status"], "READY")
            self.assertEqual(admitted, contract)
            gate = post.prepare(spec, plan, digest, (), workers, [batch._scopes(r) for r in workers])
        self.assertEqual(gate.router.sha256, f.router.sha256)
        self.assertEqual(gate.binding["source_targets"],
                         ["other.txt", "parent.txt", "solution.txt", "sources/optional.txt"])
        self.assertEqual(post._load(spec), (request, contract))
        self.assertTrue(post.claim(gate, plan))
        f.solution.write_text("after member 0")
        other.write_text("after member 1")
        parent_source.write_text("after parent")
        self.assertFalse(post.acknowledge_parent(gate)["verifier_executed"])
        with patch.object(caller, "execute", side_effect=AssertionError("provider path reached")):
            result = post.finish(gate, {"status": "RESULT_AVAILABLE"})
        self.assertEqual(result["post_batch_verifier"]["status"], "PASSED", result)
        self.assertEqual(set(result["post_batch_verifier"]["source_postimages"]),
                         {"solution.txt", "other.txt", "sources/optional.txt", "parent.txt"})
        self.assertIsNone(result["post_batch_verifier"]["source_postimages"]["sources/optional.txt"])
        self.assertEqual(f.solution.read_text(), "after member 0")
        self.assertEqual(marker.read_text(), "verified\n")
        self.assertEqual(post.recover(spec, digest, {"status": "RESULT_AVAILABLE"})[
            "post_batch_verifier"]["status"], "UNKNOWN")
        receipt_path = Path(result["post_batch_verifier"]["receipt_path"])
        receipt = json.loads(receipt_path.read_text())
        f.solution.write_text("unverified bytes")
        sources = post._capture_verification_sources(f.workspace, contract,
                                                     source_targets=gate.binding["source_targets"])
        receipt["observation"]["verification_evidence"]["source_postimages"] = {
            name: row[0] for name, row in sources.items()}
        receipt_path.write_text(json.dumps(receipt))
        self.assertEqual(post.recover(spec, digest, {"status": "RESULT_AVAILABLE"})[
            "post_batch_verifier"]["status"], "UNKNOWN")
        self.assertFalse(post.claim(gate, plan))
        self.assertEqual(marker.read_text(), "verified\n")


if __name__ == "__main__":
    unittest.main()
