# SPDX-License-Identifier: MIT
"""Synthetic full-core-shaped receipts; no execution or filesystem effects outside tempdir."""
import hashlib
import importlib.util
import io
import json
from copy import deepcopy
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import batch, file_change_evidence, full_core, result_inspection as inspector
from codex_collaboration_harness import postimage_context


RID = "tura_embedded_" + "a" * 64
THREAD = "12345678-1234-1234-1234-123456789abc"


def store(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(caller._canonical_bytes(value))


def ref(path):
    return full_core._record_artifact(path)


class ParentReviewRecordTests(unittest.TestCase):
    def setUp(self):
        self.terminal_reference = {"path": "/task-artifacts/" + RID + "/terminal.json", "sha256": "b" * 64}
        self.checks = [{"path": "/Volumes/NOKIY-TB5/UTM/runtime_repros/parent-review/" + RID + "/verification.json",
                        "sha256": "c" * 64, "check_status": "passed"},
                       {"path": "/workspace/src/file.py", "sha256": "d" * 64, "check_status": "passed"}]
        self.projected = {"schema_version": caller.TERMINAL_SCHEMA_VERSION, "request_id": RID,
                          "request_sha256": RID[-64:], "native_thread_id": THREAD,
                          "status": "RESULT_AVAILABLE", "first_typed_blocker": None, "cleanup_pass": True,
                          "continuation_owner": "parent", "authority_effect": "workspace",
                          "result_text": "source body must not be copied\n" * 10000,
                          "usage": {"large_dump": [1] * 10000},
                          "result_inspection": {"schema_version": "nokiy_result_inspection_v1",
                                                "terminal_sha256": "b" * 64, "status": "EVIDENCE_VERIFIED",
                                                "artifact_integrity": "verified", "mission_acceptance": "parent_owned",
                                                "first_blocker": None, "command_success": "known_success",
                                                "cleanup": {"historical_pass": True, "engine_pid": "absent",
                                                            "supervisor_pid": "absent"},
                                                "commands": [{"command": "do not copy me", "output": "body" * 10000}]}}

    def record(self, **overrides):
        arguments = {"expected_request_id": RID, "expected_thread_id": THREAD,
                     "terminal_reference": self.terminal_reference, "decision": "accepted",
                     "task_evidence": self.checks, "note": "Parent reviewed task checks and semantics."}
        arguments.update(overrides)
        return inspector.parent_review_record(self.projected, **arguments)

    def test_compact_projected_and_summary_output_is_detached_and_inputs_immutable(self):
        before = deepcopy((self.projected, self.terminal_reference, self.checks))
        record = self.record()
        summary = inspector.summarize_terminal(self.projected, Path("/task-artifacts"), RID)
        self.assertEqual(inspector.parent_review_record(summary, expected_request_id=RID,
                         expected_thread_id=THREAD, terminal_reference=self.terminal_reference,
                         decision="accepted", task_evidence=self.checks, note=record["note"]), record)
        encoded = json.dumps(record, separators=(",", ":"))
        self.assertLess(len(encoded.encode("utf-8")), 2500)
        for forbidden in ("result_text", "commands", "usage", "source body", "do not copy me", "large_dump"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(record["decision"], "accepted")
        self.assertEqual(record["terminal_reference"], self.terminal_reference)
        self.assertEqual(record["task_evidence"], self.checks)  # TB5 artifacts outside the workspace are retained.
        boundary = [{**self.checks[0], "path": "/" + "a/" * 511 + "b"}]
        self.assertEqual(self.record(task_evidence=boundary)["task_evidence"], boundary)  # 1024 UTF-8 bytes.
        self.assertEqual(record["scope"]["authority_effect"], "none")
        self.assertFalse(record["scope"]["permission_grant"])
        self.assertFalse(record["scope"]["deployment_or_lease_grant"])
        self.assertEqual((self.projected, self.terminal_reference, self.checks), before)
        record["task_evidence"][0]["check_status"] = "failed"
        record["terminal_reference"]["sha256"] = "e" * 64
        record["blockers"]["terminal"] = "changed"
        self.assertEqual((self.projected, self.terminal_reference, self.checks), before)

    def test_request_thread_and_original_inspection_hash_must_agree(self):
        other_thread = "87654321-1234-1234-1234-123456789abc"
        other_request = "tura_embedded_" + "f" * 64
        for target, key, value in (
                ("terminal", "request_id", other_request), ("terminal", "request_sha256", "f" * 64),
                ("terminal", "native_thread_id", other_thread), ("terminal", "terminal_sha256", "f" * 64),
                ("terminal", "terminal_path", "/foreign/" + other_request + "/terminal.json"),
                ("inspection", "request_id", other_request), ("inspection", "expected_thread_id", other_thread),
                ("inspection", "terminal_sha256", "f" * 64), ("inspection", "terminal_sha256", None)):
            with self.subTest(target=target, key=key):
                before = deepcopy(self.projected)
                (self.projected if target == "terminal" else self.projected["result_inspection"])[key] = value
                with self.assertRaises(ValueError):
                    self.record()
                self.projected = before
        for overrides in ({"expected_request_id": other_request}, {"expected_thread_id": other_thread},
                          {"terminal_reference": {**self.terminal_reference, "sha256": "f" * 64}},
                          {"terminal_reference": {**self.terminal_reference,
                                                   "path": "/task-artifacts/" + other_request + "/terminal.json"}}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.record(**overrides)
        summary = inspector.summarize_terminal(self.projected, Path("/task-artifacts"), RID)
        for key in ("terminal_sha256", "terminal_path", "terminal_schema_version"):
            incomplete = {name: value for name, value in summary.items() if name != key}
            with self.subTest(missing=key), self.assertRaises(ValueError):
                inspector.parent_review_record(incomplete, expected_request_id=RID, expected_thread_id=THREAD,
                    terminal_reference=self.terminal_reference, decision="pending", task_evidence=[], note="Pending.")

    def test_decision_is_explicit_and_failed_unknown_or_absent_checks_cannot_accept(self):
        for status in ("failed", "unknown"):
            checks = [{**self.checks[0], "check_status": status}]
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.record(task_evidence=checks)
            for decision in ("rejected", "pending"):
                record = self.record(decision=decision, task_evidence=checks, note="Checks are not acceptance.")
                self.assertEqual(record["decision"], decision)
                self.assertEqual(record["task_evidence"][0]["check_status"], status)
        with self.assertRaises(ValueError):
            self.record(task_evidence=[])
        for decision in ("pending", "rejected"):
            self.assertEqual(self.record(decision=decision, task_evidence=[])["decision"], decision)
        self.assertEqual(self.record(decision="pending", note="All mechanical checks passed.")["decision"], "pending")
        with self.assertRaises(TypeError):
            inspector.parent_review_record(self.projected, expected_request_id=RID, expected_thread_id=THREAD,
                terminal_reference=self.terminal_reference, task_evidence=self.checks, note="Passed.")

    def test_malformed_types_and_bounded_references_are_rejected(self):
        for overrides in ({"expected_request_id": None}, {"expected_thread_id": True}, {"decision": None},
                          {"decision": True}, {"decision": "accept"}, {"note": None}, {"note": []},
                          {"note": " "}, {"note": "é" * 257}, {"note": "\ud800"},
                          {"task_evidence": None}, {"task_evidence": {}}, {"task_evidence": tuple(self.checks)},
                          {"task_evidence": [True]}, {"task_evidence": self.checks * 5},
                          {"terminal_reference": []}, {"terminal_reference": {**self.terminal_reference, "body": "no"}}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.record(**overrides)
        for value in (None, True, [], {"schema_version": "foreign"}):
            with self.subTest(projected=value), self.assertRaises(ValueError):
                inspector.parent_review_record(value, expected_request_id=RID, expected_thread_id=THREAD,
                    terminal_reference=self.terminal_reference, decision="pending", task_evidence=[], note="Pending.")
        for key, value in (("path", None), ("sha256", True), ("sha256", "C" * 64), ("sha256", "c" * 63),
                           ("check_status", True), ("check_status", "skipped"), ("body", "source")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.record(task_evidence=[{**self.checks[0], key: value}])
        with self.assertRaises(ValueError):
            self.record(task_evidence=[self.checks[0], self.checks[0].copy()])
        for path in ("review/verification.json", "src/file.py", "../foreign", "a/../b", "~home/check",
                     "C:/checks.json", "/", "//a/check.json", "/a//b", "/a/./b", "/a/../b", "/a/b/",
                     "/a/~home/check", "/ a/check", "/a /check", "/a\\b", "/a:*", "/.git/config",
                     "/a\x00b", "/" + "x" * 1024, "/" + "é" * 512,
                     "/task-artifacts/tura_embedded_" + "f" * 64 + "/checks.json"):
            for decision in ("accepted", "pending", "rejected"):
                with self.subTest(path=path, decision=decision), self.assertRaises(ValueError):
                    self.record(decision=decision, task_evidence=[{**self.checks[0], "path": path}])

    def test_negative_declarations_keep_blockers_scope_and_failed_inspection_without_claiming_success(self):
        self.projected.update(status="BLOCKED", first_typed_blocker="ORIGINAL_FAILURE",
                              first_blocker="Task incomplete", capability_gap={"status": "MODEL_REPORTED",
                              "first_blocker": "MISSING_MODIFY", "unified_diff": "body must not be copied"})
        self.projected["result_inspection"].update(status="INCOMPLETE_EVIDENCE", first_blocker="COMMAND_FAILURE",
                                                 projection_partial=True, projection_blocker="PROJECTION_PARTIAL")
        for decision in ("pending", "rejected"):
            record = self.record(decision=decision, task_evidence=[])
            self.assertEqual(record["decision"], decision)
            self.assertEqual(record["inspection_status"], "INCOMPLETE_EVIDENCE")
            self.assertEqual(record["blockers"], {"terminal": "ORIGINAL_FAILURE", "terminal_detail": "Task incomplete",
                "inspection": "COMMAND_FAILURE", "projection": "PROJECTION_PARTIAL", "capability_gap": "MISSING_MODIFY"})
            self.assertTrue(record["scope"]["requires_parent_readmission"])
            self.assertTrue(record["scope"]["projection_partial"])
            self.assertNotIn("unified_diff", json.dumps(record))
        with self.assertRaises(ValueError):
            self.record()

    def test_all_decisions_require_verified_integrity_and_proven_terminal_cleanup(self):
        for target, key, value in (("terminal", "cleanup_pass", 1), ("terminal", "cleanup_pass", False),
                                  ("inspection", "artifact_integrity", "unproven"),
                                  ("inspection", "cleanup", None), ("inspection", "schema_version", "foreign"),
                                  ("inspection", "mission_acceptance", "accepted"), ("inspection", "status", None),
                                  ("cleanup", "historical_pass", 1), ("cleanup", "engine_pid", "present_unowned"),
                                  ("cleanup", "supervisor_pid", "unknown")):
            before = deepcopy(self.projected)
            destination = self.projected if target == "terminal" else self.projected["result_inspection"]
            if target == "cleanup":
                destination = destination["cleanup"]
            destination[key] = value
            for decision in ("accepted", "pending", "rejected"):
                with self.subTest(target=target, key=key, decision=decision), self.assertRaises(ValueError):
                    self.record(decision=decision)
            self.projected = before

    def test_builder_does_not_read_write_hash_probe_process_or_inspect(self):
        before = deepcopy((self.projected, self.terminal_reference, self.checks))
        with ExitStack() as stack:
            for target in ("builtins.open", "subprocess.run", "subprocess.Popen", "os.open", "os.stat",
                           "os.listdir", "os.scandir", "os.system", "os.popen", "hashlib.sha256"):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            for method in ("open", "read_bytes", "read_text", "write_bytes", "write_text", "stat", "lstat",
                           "resolve", "exists", "mkdir", "unlink"):
                stack.enter_context(patch.object(Path, method, side_effect=AssertionError(method)))
            for owner, method in ((inspector, "inspect"), (inspector.os, "kill"), (caller, "read_terminal"),
                                  (caller, "execute"), (caller, "_canonical_sha256"), (full_core, "_Trajectory"),
                                  (full_core, "_record_artifact"), (file_change_evidence, "_postimage")):
                stack.enter_context(patch.object(owner, method, side_effect=AssertionError(method)))
            record = self.record()
        self.assertEqual(record["decision"], "accepted")
        self.assertEqual((self.projected, self.terminal_reference, self.checks), before)


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        processes = patch.object(inspector.os, "kill", side_effect=ProcessLookupError())
        processes.start()
        self.addCleanup(processes.stop)
        self.root = Path(self.tmp.name).resolve()
        self.run = self.root / RID
        self.run.mkdir()
        self.command = "echo hi"
        self.receipt = {"schema_version": "tura_command_terminal_receipt_v1", "exit_code": 0,
                        "outcome": "known", "terminal_state": "completed", "process_reaped": True,
                        "process_group_empty": True, "termination_proven": True}
        self.receipt_path = self.run / "execution-state/command_receipts/call-0.json"
        store(self.receipt_path, self.receipt)
        self.item = {"type": "command_execution", "status": "completed", "command": self.command,
                     "exit_code": 0, "aggregated_output": {"exit_code": 0, "terminal_receipt": self.receipt,
                                                          "terminal_receipt_path": str(self.receipt_path)}}
        self.events = [{"type": "item.completed", "item": self.item},
                       {"type": "turn.completed", "status": "completed", "usage": {"input_tokens": 2}}]
        self.supervision = {"result": {}, "scope": {"engine_reaped": True,
                            "engine_pid": 456, "supervisor_pid": 789,
                            "no_live_descendants": True, "cleanup_error": None}}
        self.publish()

    def publish(self):
        trajectory = self.run / "core.jsonl"
        trajectory.write_bytes(b"".join(caller._canonical_bytes(event) + b"\n" for event in self.events))
        evidence = full_core._command_evidence(self.events)
        store(self.run / "command-evidence.json", evidence)
        store(self.run / "supervision.json", self.supervision)
        self.terminal = {"schema_version": caller.TERMINAL_SCHEMA_VERSION, "request_id": RID,
                         "request_sha256": RID.removeprefix("tura_embedded_"),
                         "native_thread_id": THREAD, "status": "RESULT_AVAILABLE",
                         "first_typed_blocker": None, "usage": {"input_tokens": 2},
                         "cleanup_pass": True,
                         "cleanup": self.supervision["scope"].copy(),
                         "command_evidence_summary": {k: evidence[k] for k in
                                                      ("total_count", "failed_count", "complete")},
                         "trajectory_artifact": ref(trajectory),
                         "supervision_artifact": ref(self.run / "supervision.json"),
                         "command_evidence_artifact": ref(self.run / "command-evidence.json")}
        store(self.run / "terminal.json", self.terminal)

    def inspect(self, **kwargs):
        return inspector.inspect(self.root, RID, THREAD, **kwargs)

    def cli(self, command, *, thread=THREAD):
        output = io.StringIO()
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": thread}), redirect_stdout(output):
            code = caller.main(command)
        return code, json.loads(output.getvalue())

    @contextmanager
    def loaded_request(self, request):
        # Exercise both the deployed loader and the independent source-only topology.
        with ExitStack() as stack:
            stack.enter_context(patch.object(caller, "load_request", return_value=request))
            if importlib.util.find_spec("codex_collaboration_harness.model_topology") is not None:
                stack.enter_context(patch("codex_collaboration_harness.model_topology.load_prepared_request",
                                          return_value=(request, None)))
            yield

    def capability_handoff(self):
        return {"schema_version": full_core.CAPABILITY_GAP_SCHEMA,
                "missing_capabilities": [{"path": "src/file.py", "operation": "modify", "tool": "apply_patch"}],
                "completed_work": ["Reviewed admitted source; no patch or tests executed."],
                "remaining_work": ["Apply the proposal and verify it under fresh parent admission."],
                "unified_diff": "--- a/src/file.py\n+++ b/src/file.py\n@@ -1 +1 @@\n-old\n+new\n"}

    def capability_text(self, data):
        raw = data if isinstance(data, str) else json.dumps(data)
        return "First blocker: missing capability.\n```nokiy_capability_gap_v1\n" + raw + "\n```\n"

    def capability_fixture(self, text, *, file_change=False):
        workspace = self.root / "workspace"
        (workspace / "src").mkdir(parents=True, exist_ok=True)
        (workspace / "src/file.py").write_text("old\n", encoding="utf-8")
        original = {"schema_version": caller.REQUEST_SCHEMA_VERSION, "native_thread_id": THREAD,
                    "execution_profile": "direct", "model": "gpt-6.1-sol", "reasoning_effort": "max",
                    "workspace": str(workspace), "max_result_bytes": caller.MAX_RESULT_BYTES}
        if file_change:
            contract = {"repo_root": str(workspace), "write_scopes": ["src/file.py"],
                        "declared_targets": ["src/file.py"], "allowed_operations": ["modify"],
                        "denied_operations": []}
            original["jspace_contract"] = {"sha256": caller._canonical_sha256(contract)}
        digest = caller._canonical_sha256(original)
        original.update(request_id="tura_embedded_" + digest, request_sha256=digest)
        run = self.root / original["request_id"]
        run.mkdir(exist_ok=True)
        events = [{"type": "item.completed", "item": {"type": "assistant_message", "text": text}},
                  {"type": "turn.completed", "status": "completed", "usage": self.terminal["usage"],
                   **full_core._terminal_identity(original)}]
        if file_change:
            (workspace / "src/file.py").write_text("new\n", encoding="utf-8")
            receipt_path = run / "execution-state/command_receipts/call-0.json"
            store(receipt_path, self.receipt)
            command_item = deepcopy(self.item)
            command_item["aggregated_output"]["terminal_receipt_path"] = str(receipt_path)
            events[:0] = [
                {"type": "item.completed", "item": {
                    "type": "file_change", "id": "patch-0", "status": "completed",
                    "changes": [{"kind": "update", "path": str(workspace / "src/file.py")}]}},
                {"type": "item.completed", "item": command_item},
            ]
        trajectory = run / "core.jsonl"
        trajectory.write_bytes(b"".join(caller._canonical_bytes(event) + b"\n" for event in events))
        evidence = full_core._command_evidence(events)
        store(run / "command-evidence.json", evidence)
        store(run / "supervision.json", self.supervision)
        store(run / "request-identity.json", original)
        preview, truncated = full_core._text_preview(text, caller.MAX_RESULT_BYTES)
        terminal = {**self.terminal,
                    **{key: original[key] for key in ("request_id", "request_sha256", "model",
                                                      "reasoning_effort", "execution_profile")},
                    "execution_model": "single_task_full_core", "requested_service_tier": "default",
                    "result_text": preview, "result_truncated": truncated, "result_artifact": None,
                    "trajectory_artifact": ref(trajectory), "supervision_artifact": ref(run / "supervision.json"),
                    "command_evidence_artifact": ref(run / "command-evidence.json"),
                    "command_evidence_summary": {k: evidence[k] for k in ("total_count", "failed_count", "complete")}}
        if truncated:
            (run / "last-message.txt").write_text(text, encoding="utf-8")
            terminal["result_artifact"] = ref(run / "last-message.txt")
        if file_change:
            for name in ("jspace-original.json", "jspace.json"):
                store(run / name, contract)
            proof = file_change_evidence.produce(events, SimpleNamespace(
                workspace=workspace, request_id=original["request_id"],
                request_sha256=original["request_sha256"], native_thread_id=THREAD), contract)
            store(run / "file-change-evidence.json", proof)
            terminal.update(
                file_change_evidence_artifact=ref(run / "file-change-evidence.json"),
                original_jspace_artifact=ref(run / "jspace-original.json"),
                file_change_evidence_summary={k: proof[k] for k in ("total_count", "target_count")})
        store(run / "terminal.json", terminal)
        return terminal, run, workspace

    def project_capability(self, terminal, *, thread=THREAD, **kwargs):
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": thread}):
            return inspector.project_terminal(terminal, self.root, terminal["request_id"], **kwargs)

    def delivery_request(self, index=1):
        root = self.root / f"delivery-artifacts-{index}"
        root.mkdir()
        return SimpleNamespace(artifact_root=root, request_id="tura_embedded_" + f"{index:064x}",
                               native_thread_id=THREAD, schema_version=caller.REQUEST_SCHEMA_VERSION,
                               execution_profile="direct", workspace=self.root / "workspace")

    def delivery_execute(self, request):
        """Create realistic local receipts during execute, never before the new-run guard."""
        run = request.artifact_root / request.request_id
        run.mkdir()
        receipt_path = run / "execution-state/command_receipts/call-0.json"
        store(receipt_path, self.receipt)
        events = deepcopy(self.events)
        events[0]["item"]["aggregated_output"]["terminal_receipt_path"] = str(receipt_path)
        trajectory = run / "core.jsonl"
        trajectory.write_bytes(b"".join(caller._canonical_bytes(event) + b"\n" for event in events))
        evidence = full_core._command_evidence(events)
        store(run / "command-evidence.json", evidence)
        store(run / "supervision.json", self.supervision)
        terminal = {**self.terminal, "request_id": request.request_id, "request_sha256": request.request_id[-64:],
                    "result_text": "delivered result é" * 1000, "model": "gpt-6-astra", "reasoning_effort": "ultra",
                    "model_route": {"worker_family": "native", "role": "direct", "selection": "prepared"},
                    "requested_service_tier": "priority", "observed_service_tier": "priority",
                    "usage": {**{key: 123 for key in inspector.USAGE_FIELDS},
                              "coverage": {"status": "complete", "source": "provider_observed"}},
                    "trajectory_artifact": ref(trajectory), "supervision_artifact": ref(run / "supervision.json"),
                    "command_evidence_artifact": ref(run / "command-evidence.json"),
                    "command_evidence_summary": {k: evidence[k] for k in ("total_count", "failed_count", "complete")}}
        store(run / "terminal.json", terminal)
        return terminal

    def load_summary(self, reference, **overrides):
        arguments = {"expected_request_id": RID, "expected_thread_id": THREAD,
                     "terminal_reference": {"path": str(self.run / "terminal.json"),
                                            "sha256": hashlib.sha256(caller._canonical_bytes(self.terminal)).hexdigest()}}
        arguments.update(overrides)
        return inspector.load_inspection_summary(reference, **arguments)

    def test_retained_summary_roundtrip_inspects_once_and_reuses_only_identical_bytes(self):
        terminal_before = (self.run / "terminal.json").read_bytes()
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                patch.object(inspector, "inspect", wraps=inspector.inspect) as inspected:
            projected = inspector.project_terminal(self.terminal, self.root, RID)
            original = deepcopy(projected)
            with patch.object(inspector.os, "kill", side_effect=AssertionError("no publication process pass")), \
                    patch.object(full_core, "_ArtifactReader", side_effect=AssertionError("no second trajectory pass")):
                delivered = inspector.retain_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
                reference = delivered["inspection_summary"]
                path = self.run / inspector.INSPECTION_SUMMARY_FILE
                raw, metadata = path.read_bytes(), path.stat()
                again = inspector.publish_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
                loaded = self.load_summary(reference)
        inspected.assert_called_once_with(self.root, RID, THREAD, command_stream="priority")
        self.assertEqual(projected, original)
        self.assertEqual(reference, again)
        self.assertEqual(reference, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        self.assertLessEqual(len(raw), inspector.MAX_PROJECTED_BYTES)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(path.stat().st_ino, metadata.st_ino)
        self.assertEqual(path.stat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertEqual(loaded, inspector.summarize_terminal(projected, self.root, RID))
        self.assertNotIn("inspection_summary", loaded)  # No recursive reference.
        self.assertEqual((self.run / "terminal.json").read_bytes(), terminal_before)
        checks = [{"path": str(self.root / "parent-check.json"), "sha256": "c" * 64, "check_status": "passed"}]
        record = inspector.parent_review_record(loaded, expected_request_id=RID, expected_thread_id=THREAD,
            terminal_reference={"path": loaded["terminal_path"], "sha256": loaded["terminal_sha256"]},
            decision="accepted", task_evidence=checks, note="Explicit synthetic parent decision.")
        self.assertEqual(record["decision"], "accepted")
        self.assertFalse(record["scope"]["permission_grant"])

    def test_retained_summary_and_batch_summarizers_are_pure_and_keep_references(self):
        projected = self.project_capability(self.terminal)
        delivered = inspector.retain_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
        member = batch.Member(self.root / "request.json", "c" * 64, RID, self.root)
        result = batch._aggregate((member,), {RID: batch._row(member, terminal=delivered)})
        before = deepcopy((delivered, result))
        with patch.object(inspector, "inspect", side_effect=AssertionError("pure")), \
                patch.object(inspector, "publish_inspection_summary", side_effect=AssertionError("pure")), \
                patch.object(inspector.os, "open", side_effect=AssertionError("no I/O")), \
                patch.object(inspector.os, "kill", side_effect=AssertionError("no process pass")):
            serial = inspector.summarize_terminal(delivered, self.root, RID)
            compact = batch.expand_summary(batch.summarize(result))
        self.assertEqual(serial["inspection_summary"], delivered["inspection_summary"])
        self.assertEqual(compact["members"][0]["inspection_summary"], delivered["inspection_summary"])
        self.assertEqual((delivered, result), before)

    def test_failed_and_partial_snapshots_preserve_blockers_and_cannot_be_accepted(self):
        projected = self.project_capability(self.terminal)
        for index, (status, integrity, blocker, partial) in enumerate((
                ("INCOMPLETE_EVIDENCE", "verified", "COMMAND_FAILURE", False),
                ("INCOMPLETE_EVIDENCE", "verified", "PROJECTION_PARTIAL", True),
                ("INCOMPLETE_EVIDENCE", "unproven", "ARTIFACT_HASH_MISMATCH", False))):
            root = self.root / f"partial-{index}"
            (root / RID).mkdir(parents=True)
            value = deepcopy(projected)
            value.update(status="BLOCKED", first_typed_blocker="ORIGINAL_EXECUTION_BLOCKER")
            value["result_inspection"].update(status=status, artifact_integrity=integrity, first_blocker=blocker,
                                                command_success="known_failure", projection_partial=partial)
            if partial:
                value["result_inspection"]["projection_blocker"] = "PROJECTION_PARTIAL"
            delivered = inspector.retain_inspection_summary(value, root, RID, expected_thread_id=THREAD)
            summary = inspector.load_inspection_summary(delivered["inspection_summary"], expected_request_id=RID,
                expected_thread_id=THREAD, terminal_reference={"path": str(root / RID / "terminal.json"),
                "sha256": value["result_inspection"]["terminal_sha256"]})
            self.assertEqual(summary["status"], "BLOCKED")
            self.assertEqual(summary["first_typed_blocker"], "ORIGINAL_EXECUTION_BLOCKER")
            self.assertEqual(summary["result_inspection"], value["result_inspection"])
            with self.assertRaises(ValueError):
                inspector.parent_review_record(summary, expected_request_id=RID, expected_thread_id=THREAD,
                    terminal_reference={"path": summary["terminal_path"], "sha256": summary["terminal_sha256"]},
                    decision="accepted", task_evidence=[{"path": str(root / "check.json"), "sha256": "c" * 64,
                                                        "check_status": "passed"}], note="Cannot upgrade failure.")
            if integrity == "unproven":
                with self.assertRaises(ValueError):
                    inspector.parent_review_record(summary, expected_request_id=RID, expected_thread_id=THREAD,
                        terminal_reference={"path": summary["terminal_path"], "sha256": summary["terminal_sha256"]},
                        decision="pending", task_evidence=[], note="Unverified integrity remains unverified.")

    def test_retained_summary_loader_rejects_wrong_expected_identity_hash_size_and_path(self):
        reference = inspector.publish_inspection_summary(self.project_capability(self.terminal), self.root, RID,
                                                         expected_thread_id=THREAD)
        for overrides in ({"expected_request_id": "tura_embedded_" + "b" * 64},
                          {"expected_request_id": "../" + RID}, {"expected_request_id": True},
                          {"expected_thread_id": "87654321-1234-1234-1234-123456789abc"},
                          {"expected_thread_id": None},
                          {"terminal_reference": {"path": str(self.run / "terminal.json"), "sha256": "c" * 64}},
                          {"terminal_reference": {"path": str(self.run / "other.json"), "sha256": "c" * 64}}):
            with self.subTest(overrides=overrides), self.assertRaises((ValueError, OSError)):
                self.load_summary(reference, **overrides)
        for changes in ({"sha256": "c" * 64}, {"sha256": "C" * 64}, {"bytes": reference["bytes"] + 1},
                        {"bytes": True}, {"bytes": 0}, {"bytes": inspector.MAX_PROJECTED_BYTES + 1},
                        {"path": str(self.run / "other.json")},
                        {"path": str(self.run / ".." / RID / inspector.INSPECTION_SUMMARY_FILE)},
                        {"path": "relative/inspection-summary.json"}, {"body": "not a reference"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.load_summary({**reference, **changes})
        path = Path(reference["path"])
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b"EVIDENCE_VERIFIED", b"EVIDENCE_TAMPERED"))
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            self.load_summary(reference)

    def test_retained_summary_loader_rejects_self_consistent_foreign_or_malformed_snapshots(self):
        reference = inspector.publish_inspection_summary(self.project_capability(self.terminal), self.root, RID,
                                                         expected_thread_id=THREAD)
        path = Path(reference["path"])
        original = json.loads(path.read_bytes())
        cases = [("request_id", "tura_embedded_" + "b" * 64), ("request_sha256", "b" * 64),
                 ("native_thread_id", "87654321-1234-1234-1234-123456789abc"),
                 ("terminal_sha256", "c" * 64), ("terminal_path", str(self.root / "foreign/terminal.json")),
                 ("terminal_schema_version", "foreign"), ("schema_version", "foreign")]
        for key, value in cases:
            snapshot = {**original, key: value}
            raw = caller._canonical_bytes(snapshot)
            path.write_bytes(raw)
            bound = {**reference, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "IDENTITY_MISMATCH"):
                self.load_summary(bound)
        for key, value in (("terminal_sha256", "c" * 64), ("expected_thread_id", "foreign"), ("request_id", "foreign")):
            snapshot = deepcopy(original)
            snapshot["result_inspection"][key] = value
            raw = caller._canonical_bytes(snapshot)
            path.write_bytes(raw)
            with self.subTest(inspection=key), self.assertRaisesRegex(ValueError, "IDENTITY_MISMATCH"):
                self.load_summary({**reference, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        for raw in (b'{"schema_version":"x","schema_version":"y"}', b'{"nested":{"x":1,"x":2}}',
                    b'{"n":NaN}', b'[]', b'\xff'):
            path.write_bytes(raw)
            with self.subTest(raw=raw[:50]), self.assertRaisesRegex(ValueError, "INVALID_JSON"):
                self.load_summary({**reference, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        # A finite deeply nested document may parse under a raised recursion limit,
        # but cannot bypass the snapshot schema/identity checks either way.
        raw = b'{"nested":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
        path.write_bytes(raw)
        with self.assertRaises(ValueError):
            self.load_summary({**reference, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        path.write_bytes(b" " * (inspector.MAX_PROJECTED_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "OVERSIZED"):
            self.load_summary(reference)

    def test_summary_parent_components_and_final_symlinks_are_never_followed(self):
        projected = self.project_capability(self.terminal)
        reference = inspector.publish_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
        for alias_root in (self.root / "root-alias", self.root / "run-alias"):
            if alias_root.name == "root-alias":
                alias_root.symlink_to(self.root, target_is_directory=True)
            else:
                alias_root.mkdir()
                (alias_root / RID).symlink_to(self.run, target_is_directory=True)
            before = Path(reference["path"]).read_bytes()
            with self.subTest(alias=alias_root), patch.object(caller, "_write_create_only",
                                                            side_effect=AssertionError("unsafe parent")):
                result = inspector.retain_inspection_summary(projected, alias_root, RID, expected_thread_id=THREAD)
                self.assertIsNone(result["inspection_summary"])
                with self.assertRaises((OSError, ValueError)):
                    inspector.load_inspection_summary({**reference, "path": str(alias_root / RID / inspector.INSPECTION_SUMMARY_FILE)},
                        expected_request_id=RID, expected_thread_id=THREAD,
                        terminal_reference={"path": str(alias_root / RID / "terminal.json"),
                                            "sha256": projected["result_inspection"]["terminal_sha256"]})
            self.assertEqual(Path(reference["path"]).read_bytes(), before)
        snapshot = Path(reference["path"])
        snapshot.unlink()
        foreign = self.root / "foreign.json"
        foreign.write_bytes(b"must remain untouched")
        snapshot.symlink_to(foreign)
        result = inspector.retain_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
        self.assertIsNone(result["inspection_summary"])
        self.assertEqual(foreign.read_bytes(), b"must remain untouched")
        with self.assertRaises(ValueError):
            self.load_summary(reference)
        for root in (Path("relative"), self.root / ".." / self.root.name, self.root / ".git"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                inspector.publish_inspection_summary(projected, root, RID, expected_thread_id=THREAD)

    def test_summary_final_file_race_and_nonregular_collision_fail_closed(self):
        projected = self.project_capability(self.terminal)
        reference = inspector.publish_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
        snapshot = Path(reference["path"])
        foreign = self.root / "foreign.json"
        foreign.write_bytes(snapshot.read_bytes())
        opened = inspector.os.open

        def replace_before_open(path, flags, *args, **kwargs):
            if path == Path(inspector.INSPECTION_SUMMARY_FILE) and not flags & inspector.os.O_CREAT:
                snapshot.unlink()
                snapshot.symlink_to(foreign)
            return opened(path, flags, *args, **kwargs)

        with patch.object(inspector.os, "open", side_effect=replace_before_open), self.assertRaises(ValueError):
            self.load_summary(reference)
        snapshot.unlink()
        snapshot.mkdir()
        delivered = inspector.retain_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
        self.assertIsNone(delivered["inspection_summary"])
        self.assertTrue(snapshot.is_dir())

    def test_interrupted_publication_and_collision_never_overwrite_or_replace_result(self):
        projected = self.project_capability(self.terminal)
        projected.update(first_typed_blocker="ORIGINAL_BLOCKER", first_blocker="Original detail")
        before = deepcopy(projected)

        def interrupted(path, value, *, dir_fd):
            fd = inspector.os.open(path, inspector.os.O_CREAT | inspector.os.O_EXCL | inspector.os.O_WRONLY,
                                   0o600, dir_fd=dir_fd)
            with inspector.os.fdopen(fd, "wb") as stream:
                stream.write(b'{"interrupted":')
            raise OSError("unbounded exception must not escape " * 10000)

        with patch.object(caller, "_write_create_only", side_effect=interrupted):
            delivered = inspector.retain_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
        self.assertIsNone(delivered["inspection_summary"])
        self.assertLess(len(delivered["inspection_summary_diagnostic"].encode()), 128)
        path = self.run / inspector.INSPECTION_SUMMARY_FILE
        raw, metadata = path.read_bytes(), path.stat()
        collided = inspector.retain_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
        self.assertEqual(collided["inspection_summary_diagnostic"], "INSPECTION_SUMMARY_COLLISION")
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertEqual(projected, before)
        for result in (delivered, collided):
            for key, value in before.items():
                self.assertEqual(result[key], value)

    def test_conflicting_valid_snapshot_and_oversized_projection_are_not_published(self):
        projected = self.project_capability(self.terminal)
        reference = inspector.publish_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
        path = Path(reference["path"])
        before = path.read_bytes()
        conflict = {**projected, "result_text": "Different already computed projection"}
        result = inspector.retain_inspection_summary(conflict, self.root, RID, expected_thread_id=THREAD)
        self.assertEqual(result["inspection_summary_diagnostic"], "INSPECTION_SUMMARY_COLLISION")
        self.assertEqual(path.read_bytes(), before)
        huge = {**projected, "result_text": "é" * inspector.MAX_PROJECTED_BYTES}
        with patch.object(caller, "_write_create_only", side_effect=AssertionError("oversize must not write")):
            result = inspector.retain_inspection_summary(huge, self.root, RID, expected_thread_id=THREAD)
        self.assertEqual(result["inspection_summary_diagnostic"], "INSPECTION_SUMMARY_TOO_LARGE")
        self.assertEqual(result["status"], huge["status"])
        self.assertEqual(result["result_inspection"], huge["result_inspection"])

    def test_serial_new_run_publishes_once_in_full_and_compact_paths(self):
        for index, summary_flag in enumerate(([], ["--summary"]), start=1):
            request = self.delivery_request(index)
            with self.loaded_request(request), patch.object(caller, "execute", side_effect=self.delivery_execute) as execute, \
                    patch.object(inspector, "inspect", wraps=inspector.inspect) as inspected:
                code, result = self.cli(["run", "--request", str(self.root / "unused.json"), *summary_flag])
            self.assertEqual(code, 0)
            execute.assert_called_once_with(request)
            inspected.assert_called_once()
            reference = result["inspection_summary"]
            self.assertEqual(reference["path"], str(request.artifact_root / request.request_id / inspector.INSPECTION_SUMMARY_FILE))
            terminal = caller.read_terminal(request.artifact_root, request.request_id)
            self.assertNotIn("result_inspection", terminal)
            loaded = inspector.load_inspection_summary(reference, expected_request_id=request.request_id,
                expected_thread_id=THREAD, terminal_reference={"path": str(request.artifact_root / request.request_id / "terminal.json"),
                "sha256": result["result_inspection"]["terminal_sha256"]})
            self.assertEqual(loaded["result_inspection"], result["result_inspection"])

    def test_serial_publication_failure_preserves_success_or_original_failure_and_exit(self):
        for index, failed in enumerate((False, True), start=1):
            request = self.delivery_request(index)
            if failed:
                self.terminal.update(status="BLOCKED", first_typed_blocker="ORIGINAL_EXECUTION_FAILURE")
            with self.loaded_request(request), patch.object(caller, "execute", side_effect=self.delivery_execute) as execute, \
                    patch.object(caller, "_write_create_only", side_effect=OSError("publication unavailable")):
                code, result = self.cli(["run", "--request", str(self.root / "unused.json"), "--summary"])
            self.assertEqual(code, 2 if failed else 0)
            self.assertEqual(result["status"], self.terminal["status"])
            self.assertEqual(result["first_typed_blocker"], self.terminal["first_typed_blocker"])
            self.assertIsNone(result["inspection_summary"])
            self.assertEqual(result["inspection_summary_diagnostic"], "INSPECTION_SUMMARY_PUBLICATION_FAILED")
            execute.assert_called_once_with(request)
            self.assertTrue((request.artifact_root / request.request_id / "terminal.json").is_file())

    def test_serial_read_and_existing_run_recovery_never_publish_or_require_snapshot(self):
        request = SimpleNamespace(artifact_root=self.root, request_id=RID, native_thread_id=THREAD)
        snapshot = self.run / inspector.INSPECTION_SUMMARY_FILE
        before = {path: path.read_bytes() for path in self.run.rglob("*") if path.is_file()}
        with self.loaded_request(request), patch.object(caller, "execute", return_value=self.terminal), \
                patch.object(inspector, "publish_inspection_summary", side_effect=AssertionError("read-only")), \
                patch.object(caller, "_write_create_only", side_effect=AssertionError("read-only")):
            for command in (["read-result", "--artifact-root", str(self.root), "--request-id", RID],
                            ["run", "--request", str(self.root / "unused.json")]):
                code, result = self.cli([*command, "--summary"])
                self.assertEqual(code, 0)
                self.assertIsNone(result["inspection_summary"])
                self.assertEqual(result["inspection_summary_omission"], "READ_ONLY_RECOVERY")
            self.assertFalse(snapshot.exists())
            snapshot.write_bytes(b"interrupted legacy snapshot")
            code, result = self.cli(["read-result", "--artifact-root", str(self.root), "--request-id", RID])
        self.assertEqual(code, 0)
        self.assertEqual(snapshot.read_bytes(), b"interrupted legacy snapshot")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_actual_batch_execute_one_publishes_all_five_once_and_compact_references_fit(self):
        requests = [self.delivery_request(i) for i in range(1, 6)]
        requests[0].workspace.mkdir()
        members, prepared = [], {}
        for index, request in enumerate(requests):
            path = self.root / f"request-{index}.json"
            raw = str(index).encode()
            path.write_bytes(raw)
            members.append(batch.Member(path, hashlib.sha256(raw).hexdigest(), request.request_id, request.artifact_root))
            prepared[path] = (request, raw)
        members = tuple(members)
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                patch.object(batch, "_load_plan", return_value=(members, 5)), \
                patch.object(batch, "load_prepared_request", side_effect=lambda path: prepared[path]), \
                patch.object(batch, "worker_capacity", return_value=5), \
                patch.object(batch, "_scopes", return_value=(set(), set())), \
                patch.object(caller, "preflight", return_value={"status": "READY"}), \
                patch.object(caller, "execute", side_effect=self.delivery_execute) as execute, \
                patch.object(inspector, "inspect", wraps=inspector.inspect) as inspected:
            result = batch.run_batch(self.root / "unused-plan.json", "b" * 64)
        self.assertEqual(execute.call_count, 5)
        self.assertEqual(inspected.call_count, 5)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        before = deepcopy(result)
        with patch.object(inspector, "inspect", side_effect=AssertionError("no repeated inspection")), \
                patch.object(inspector.os, "kill", side_effect=AssertionError("no live clearance")), \
                patch.object(full_core, "_ArtifactReader", side_effect=AssertionError("no trajectory pass")):
            compact = batch.summarize(result)
            self.assertLessEqual(len(json.dumps(compact, ensure_ascii=True, sort_keys=True).encode("ascii")), 8000)
            expanded = batch.expand_summary(compact)
            self.assertEqual(len(expanded["members"]), 5)
            for row, source in zip(expanded["members"], result["members"]):
                reference = row["inspection_summary"]
                self.assertEqual(reference, source["terminal"]["inspection_summary"])
                loaded = inspector.load_inspection_summary(reference, expected_request_id=row["request_id"],
                    expected_thread_id=THREAD, terminal_reference={"path": row["terminal_path"], "sha256": row["terminal_sha256"]})
                self.assertEqual(loaded["result_inspection"], source["terminal"]["result_inspection"])
                self.assertLessEqual(reference["bytes"], inspector.MAX_PROJECTED_BYTES)
        self.assertEqual(result, before)

    def test_batch_publication_failure_keeps_result_and_result_only_recovery_does_not_write(self):
        request = self.delivery_request()
        request.workspace.mkdir()
        raw = b"prepared request"
        member = batch.Member(self.root / "request.json", hashlib.sha256(raw).hexdigest(), request.request_id, request.artifact_root)
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                patch.object(batch, "_load_plan", return_value=((member,), 1)), \
                patch.object(batch, "load_plan", return_value=(member,)), \
                patch.object(batch, "load_prepared_request", return_value=(request, raw)), \
                patch.object(batch, "worker_capacity", return_value=5), \
                patch.object(batch, "_scopes", return_value=(set(), set())), \
                patch.object(caller, "preflight", return_value={"status": "READY"}), \
                patch.object(caller, "execute", side_effect=self.delivery_execute) as execute, \
                patch.object(caller, "_write_create_only", side_effect=OSError("cannot retain summary")):
            result = batch.run_batch(self.root / "unused-plan.json", "b" * 64)
            self.assertEqual(result["status"], "RESULT_AVAILABLE")
            terminal = result["members"][0]["terminal"]
            self.assertIsNone(terminal["inspection_summary"])
            self.assertEqual(terminal["inspection_summary_diagnostic"], "INSPECTION_SUMMARY_PUBLICATION_FAILED")
            execute.assert_called_once_with(request)
            run = request.artifact_root / request.request_id
            before = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
            with patch.object(caller, "execute", side_effect=AssertionError("no replay")), \
                    patch.object(caller, "preflight", side_effect=AssertionError("no preflight")), \
                    patch.object(inspector, "publish_inspection_summary", side_effect=AssertionError("no recovery write")):
                recovered = [batch.read_batch(self.root / "unused-plan.json", "b" * 64),
                             batch.run_batch(self.root / "unused-plan.json", "b" * 64)]
        for value in recovered:
            self.assertEqual(value["status"], "RESULT_AVAILABLE")
            self.assertEqual(value["members"][0]["terminal"]["inspection_summary_omission"], "READ_ONLY_RECOVERY")
        self.assertEqual(before, {path: path.read_bytes() for path in run.rglob("*") if path.is_file()})

    def test_parent_review_record_consumes_real_projection_and_summary_without_reinspection(self):
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                patch.object(inspector, "inspect", wraps=inspector.inspect) as inspected:
            projected = inspector.project_terminal(self.terminal, self.root, RID)
        inspected.assert_called_once()
        summary = inspector.summarize_terminal(projected, self.root, RID)
        original = deepcopy((projected, summary))
        reference = {"path": summary["terminal_path"], "sha256": summary["terminal_sha256"]}
        checks = [{"path": str(self.root / RID / "review/check.json"),
                   "sha256": "c" * 64, "check_status": "passed"}]
        with patch.object(inspector, "inspect", side_effect=AssertionError("no second inspection")), \
                patch.object(Path, "open", side_effect=AssertionError("no artifact reads")), \
                patch.object(inspector.os, "kill", side_effect=AssertionError("no process probe")):
            records = [inspector.parent_review_record(value, expected_request_id=RID, expected_thread_id=THREAD,
                       terminal_reference=reference, decision="accepted", task_evidence=checks,
                       note="Synthetic parent declaration; not independent proof.") for value in (projected, summary)]
        self.assertEqual(records[0], records[1])
        self.assertEqual((projected, summary), original)

    def test_file_change_postimages_survive_projection_and_summary_without_extra_reads(self):
        terminal, run, workspace = self.capability_fixture("changed source", file_change=True)
        proof = json.loads((run / "file-change-evidence.json").read_text())
        before = {path: path.read_bytes() for path in run.iterdir() if path.is_file()}
        before_terminal = deepcopy(terminal)
        with patch.object(inspector, "_artifact", wraps=inspector._artifact) as artifacts, \
                patch.object(inspector, "inspect", wraps=inspector.inspect) as inspected, \
                patch.object(file_change_evidence, "_postimage", side_effect=AssertionError("no source reread")), \
                patch.object(caller, "execute", side_effect=AssertionError("no execution")):
            projected = self.project_capability(terminal)
            with patch.object(inspector, "inspect", side_effect=AssertionError("no second inspection")):
                summary = inspector.summarize_terminal(projected, self.root, terminal["request_id"])
        inspected.assert_called_once()
        self.assertEqual(sum(call.args[1] == "file-change-evidence.json"
                             for call in artifacts.call_args_list), 1)
        evidence = summary["result_inspection"]
        self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(evidence["file_change_success"], "known_success")
        self.assertEqual(evidence["command_success"], "known_success")
        self.assertEqual(evidence["mission_acceptance"], "parent_owned")
        self.assertEqual(evidence["file_changes"], {
            "events": 1, "targets": 1, "postimages": proof["postimages"]})
        self.assertEqual(proof["postimages"][0]["path"], "src/file.py")
        self.assertEqual(proof["postimages"][0]["sha256"], hashlib.sha256(b"new\n").hexdigest())
        self.assertEqual(terminal, before_terminal)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual((workspace / "src/file.py").read_bytes(), b"new\n")

    def test_file_change_postimages_do_not_assert_live_source_freshness_or_acceptance(self):
        terminal, run, workspace = self.capability_fixture("changed source", file_change=True)
        stored = json.loads((run / "file-change-evidence.json").read_text())["postimages"]
        path = workspace / "src/file.py"
        path.write_text("later parent changes\n", encoding="utf-8")
        with patch.object(file_change_evidence, "_postimage", side_effect=AssertionError("no source reread")):
            evidence = inspector.inspect(self.root, terminal["request_id"], THREAD)
        self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(evidence["file_changes"]["postimages"], stored)
        self.assertNotEqual(stored[0]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(evidence["mission_acceptance"], "parent_owned")

    def test_file_change_postimages_remain_unavailable_for_invalid_evidence(self):
        cases = (("artifact", "ARTIFACT_HASH_MISMATCH"),
                 ("postimage", "FILE_CHANGE_POSTIMAGE_INVALID"),
                 ("summary", "FILE_CHANGE_COUNTS_MISMATCH"),
                 ("contract", "FILE_CHANGE_CONTRACT_MISMATCH"))
        for case, blocker in cases:
            with self.subTest(case=case):
                terminal, run, _ = self.capability_fixture("changed source", file_change=True)
                path = run / "file-change-evidence.json"
                if case == "artifact":
                    path.write_bytes(path.read_bytes() + b" ")
                elif case == "postimage":
                    proof = json.loads(path.read_text())
                    proof["postimages"][0]["sha256"] = "x" * 64
                    store(path, proof)
                    terminal["file_change_evidence_artifact"] = ref(path)
                elif case == "summary":
                    terminal["file_change_evidence_summary"]["target_count"] = 2
                else:
                    store(run / "jspace.json", {"repo_root": "different"})
                store(run / "terminal.json", terminal)
                evidence = self.project_capability(terminal)["result_inspection"]
                self.assertEqual(evidence["status"], "INCOMPLETE_EVIDENCE")
                self.assertEqual(evidence["first_blocker"], blocker)
                self.assertEqual(evidence["file_change_success"], "unproven")
                self.assertIsNone(evidence["file_changes"])

    def test_file_change_postimages_do_not_displace_command_or_projection_evidence(self):
        terminal, _, _ = self.capability_fixture("changed source", file_change=True)
        full = inspector.inspect(self.root, terminal["request_id"], THREAD)
        baseline = deepcopy(full)
        baseline["file_changes"].pop("postimages")
        page_limit = len(caller._canonical_bytes(baseline)) + 32
        with patch.object(inspector, "MAX_PAGE_BYTES", page_limit):
            bounded = inspector.inspect(self.root, terminal["request_id"], THREAD)
        self.assertEqual(bounded, baseline)
        self.assertLessEqual(len(caller._canonical_bytes(bounded)), page_limit)

        projected = self.project_capability(terminal)
        baseline = deepcopy(projected)
        baseline["result_inspection"]["file_changes"].pop("postimages")
        # The serial CLI adds recovery metadata after this projection; it must
        # fit without displacing command evidence for optional source identities.
        delivery = {**baseline, "inspection_summary": None, "inspection_summary_omission": "READ_ONLY_RECOVERY"}
        projection_limit = len(caller._canonical_bytes(delivery)) + 32
        with patch.object(inspector, "MAX_PROJECTED_BYTES", projection_limit):
            bounded = self.project_capability(terminal)
        self.assertEqual(bounded, baseline)
        self.assertLessEqual(len(caller._canonical_bytes(bounded)), projection_limit)

    def parent_context_fixture(self, mutate=None, *, contents=None, mode="evidence_only",
                               changed=True, batched=False, terminal_status="done"):
        terminal, old_run, workspace = self.capability_fixture("planning")
        original = json.loads((old_run / "request-identity.json").read_text())
        original = {k: v for k, v in original.items() if k not in ("request_id", "request_sha256")}
        if mode is not None:
            original["terminal_delivery"] = mode
        contents = contents if contents is not None else {"src/file.py": "new café\nsecond\n"}
        contract = {"repo_root": str(workspace), "source_read": True, "read_scopes": list(contents),
                    "write_scopes": list(contents), "declared_targets": list(contents),
                    "allowed_operations": ["command", "read", "modify"], "denied_operations": []}
        sources = []
        for index, (path, text) in enumerate(contents.items()):
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
            total = text.count("\n") + int(bool(text) and not text.endswith("\n"))
            item = self.source_read_item()
            item["command"] = json.dumps({"path": path, "start_line": 1, "end_line": max(total, 1)})
            item["aggregated_output"].update(
                path=path, source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                start_line=1, end_line=total, total_lines=total, stdout=text, line_numbers=False,
                ends_with_newline=text.endswith("\n"), terminal_receipt=dict(
                    self.receipt, termination_origin="in_process_source_read", call_id=f"context-read-{index}"))
            sources.append(item)
        events = []
        if changed:
            events.append({"type": "item.completed", "item": {
                "type": "file_change", "id": "review-patch", "status": "completed",
                "changes": [{"kind": "update", "path": str(workspace / path)} for path in contents]}})
        events.append({"type": "item.completed", "item": deepcopy(self.item)})
        if batched and sources:
            sources = [self.batch_item(*sources)]
        events.extend({"type": "item.completed", "item": item} for item in sources)
        if mutate is not None:
            mutate(events, original, contract)
        original["jspace_contract"] = {"sha256": caller._canonical_sha256(contract)}
        digest = caller._canonical_sha256(original)
        original.update(request_id="tura_embedded_" + digest, request_sha256=digest)
        run = self.root / original["request_id"]
        run.mkdir(exist_ok=True)
        receipt_index = 0
        def bind_receipts(value):
            nonlocal receipt_index
            if isinstance(value, dict):
                if isinstance(value.get("terminal_receipt"), dict):
                    path = run / f"execution-state/command_receipts/context-{receipt_index}.json"
                    receipt_index += 1
                    store(path, value["terminal_receipt"])
                    value["terminal_receipt_path"] = str(path)
                for child in list(value.values()):
                    bind_receipts(child)
            elif isinstance(value, list):
                for child in value:
                    bind_receipts(child)
        bind_receipts(events)
        marker = None
        if mode == "evidence_only":
            marker = {"type": "nokiy.terminal_evidence", "schema_version": "nokiy_terminal_evidence_v1",
                      "session_id": "full-" + digest, "runtime_id": "fixture-runtime",
                      "terminal_status": terminal_status, "delivery_mode": "evidence_only",
                      "parent_acceptance_required": True, "final_summary_turn_executed": False}
            events.append(marker)
            text = caller._canonical_bytes({k: marker[k] for k in (
                "terminal_status", "delivery_mode", "parent_acceptance_required",
                "final_summary_turn_executed")}).decode()
        else:
            text = "changed source"
            events.append({"type": "item.completed", "item": {"type": "assistant_message", "text": text}})
        events.append({"type": "turn.completed", "status": "completed", "usage": terminal["usage"],
                       **full_core._terminal_identity(original)})
        terminal.update(request_id=original["request_id"], request_sha256=digest, result_text=text,
                        result_truncated=False, result_artifact=None)
        if mode == "evidence_only":
            terminal.update(requested_terminal_delivery=mode, observed_terminal_delivery=mode,
                            terminal_evidence=marker)
        store(run / "request-identity.json", original)
        store(run / "supervision.json", self.supervision)
        terminal["supervision_artifact"] = ref(run / "supervision.json")
        for name in ("jspace-original.json", "jspace.json"):
            store(run / name, contract)
        if changed:
            proof = file_change_evidence.produce(events, SimpleNamespace(
                workspace=workspace, request_id=original["request_id"], request_sha256=digest,
                native_thread_id=THREAD), contract)
            store(run / "file-change-evidence.json", proof)
            terminal.update(file_change_evidence_artifact=ref(run / "file-change-evidence.json"),
                            original_jspace_artifact=ref(run / "jspace-original.json"),
                            file_change_evidence_summary={k: proof[k] for k in ("total_count", "target_count")})
        self.republish_terminal_evidence(terminal, run, events)
        return terminal, run, workspace

    def verifier_context_fixture(self, mutate=None, *, schema=postimage_context.DELTA_NAVIGATION_SCHEMA_VERSION,
                                 contents=None, before_contents=None, include_read=False, batched=False):
        contents = contents if contents is not None else {"src/file.py": "def changed():\n    return 'new café'\n"}
        before_contents = before_contents if before_contents is not None else {p: "old\n" for p in contents}
        def prepare(events, original, contract):
            grant = {"argv": ["/python", "/public-test.py"], "scratch_root": "/scratch",
                     "timeout_seconds": 7, "pinned_files": [{"path": "/public-test.py", "sha256": "b" * 64}],
                     "python_import_roots": [str(Path(original["workspace"]) / "src")]}
            contract.update(authorization_semantic_sha256="a" * 64, verifier_commands=[grant],
                dcf_generation={"source_snapshot": {p: {
                    "sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text.encode()), "mode": 0o644}
                    for p, text in before_contents.items()}})
            files = []
            for path, text in contents.items():
                old = before_contents[path]
                count = len(postimage_context._lines(text))
                row = {"path": path, "preimage_sha256": hashlib.sha256(old.encode()).hexdigest(),
                       "postimage_sha256": hashlib.sha256(text.encode()).hexdigest()}
                if schema == postimage_context.SCHEMA_VERSION:
                    row["spans"] = [{"start_line": 1, "end_line": count, "text": text}]
                else:
                    row.update(coverage="all_text_changes", hunks=[{
                        "before_start_line": 1, "before_line_count": len(postimage_context._lines(old)),
                        "after_start_line": 1, "after_line_count": count, "before_text": old, "after_text": text}])
                if schema != postimage_context.DELTA_SCHEMA_VERSION:
                    row["locators"] = [{"qualified_name": "changed", "kind": "function", "line": 1,
                                        "start_line": 1, "end_line": count}]
                    if schema == postimage_context.SCHEMA_VERSION:
                        row["locators"][0]["complete"] = True
                files.append(row)
            definition = schema == postimage_context.SCHEMA_VERSION
            notice = {postimage_context.SCHEMA_VERSION: postimage_context.NOTICE,
                      postimage_context.DELTA_SCHEMA_VERSION: postimage_context.DELTA_NOTICE,
                      postimage_context.DELTA_NAVIGATION_SCHEMA_VERSION: postimage_context.DELTA_NAVIGATION_NOTICE}[schema]
            context = {"schema_version": schema,
                       "kind": "verifier_postimage_context" if definition else "verifier_postimage_delta",
                       "jspace_semantic_sha256": contract["authorization_semantic_sha256"],
                       "notice": notice, "files": files}
            verifier = self.verifier_item()
            payload = verifier["aggregated_output"]
            payload.update(executor="parent_focused_verifier", source_postimages=context,
                           terminal_receipt=dict(self.receipt, termination_origin="parent_verifier",
                                                 call_id="review-verifier-0", failure_class="none"),
                           verification_evidence={
                               "schema_version": inspector.VERIFICATION_EVIDENCE_SCHEMA,
                               "authorization_semantic_sha256": contract["authorization_semantic_sha256"],
                               "verifier_index": 0, "verifier_sha256": caller._canonical_sha256(grant),
                               "call_id": "review-verifier-0", "source_postimages": {
                                   p: file_change_evidence._postimage(Path(original["workspace"]), p) for p in contents}})
            reads = events[2:] if include_read else []
            if batched:
                status = deepcopy(self.item)
                status.update(command_type="task_status", command="{}")
                item = self.batch_item(*(e["item"] for e in reads), *([] if reads else [status]), verifier)
                payload = item["aggregated_output"]["results"][-1]["output"]
                events[2:] = [{"type": "item.completed", "item": item}]
            else:
                events[2:] = [{"type": "item.completed", "item": verifier}, *reads]
            if mutate is not None:
                mutate(dict(events=events, original=original, contract=contract, verifier=verifier,
                            payload=payload, context=payload["source_postimages"]))
        return self.parent_context_fixture(prepare, contents=contents)

    def repeat_review_verifier(self, state, call_id="review-verifier-1"):
        item = deepcopy(state["verifier"])
        payload = item["aggregated_output"]
        payload["terminal_receipt"]["call_id"] = call_id
        payload["verification_evidence"]["call_id"] = call_id
        state["events"].append({"type": "item.completed", "item": item})
        return payload

    def test_verifier_context_all_schemas_single_native_and_batch_reuse_verified_pass(self):
        schemas = (postimage_context.SCHEMA_VERSION, postimage_context.DELTA_SCHEMA_VERSION,
                   postimage_context.DELTA_NAVIGATION_SCHEMA_VERSION)
        for schema in schemas:
            for form in ("single", "native", "batch"):
                with self.subTest(schema=schema, form=form):
                    def native(state):
                        if form == "native":
                            state["verifier"].pop("command_type")
                    terminal, run, _ = self.verifier_context_fixture(native, schema=schema, batched=form == "batch")
                    iterations = []
                    original_iter = full_core._Trajectory.__iter__
                    def counted(trajectory):
                        iterations.append(trajectory)
                        return original_iter(trajectory)
                    with patch.object(full_core._Trajectory, "__iter__", counted), \
                            patch.object(inspector, "inspect", wraps=inspector.inspect) as inspected, \
                            patch.object(file_change_evidence, "_postimage", side_effect=AssertionError("no source reread")), \
                            patch.object(postimage_context, "project", side_effect=AssertionError("no regeneration")), \
                            patch.object(caller, "execute", side_effect=AssertionError("no execution")):
                        projected = self.project_capability(terminal)
                    inspected.assert_called_once()
                    self.assertEqual(len(iterations), 3)
                    evidence = projected["result_inspection"]
                    self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
                    context = evidence["parent_review_context"]
                    self.assertEqual(context["ranges"], [])
                    self.assertEqual(len(context["verifier_contexts"]), 1)
                    row = context["verifier_contexts"][0]
                    source = row["source_event"]
                    events = [json.loads(line) for line in (run / "core.jsonl").read_text().splitlines()]
                    event = events[source["event_index"]]
                    output = event["item"]["aggregated_output"]
                    if form == "batch":
                        output = output["results"][source["part_index"]]["output"]
                    self.assertEqual(row["source_postimages"], output["source_postimages"])
                    self.assertEqual(source["event_sha256"], caller._canonical_sha256(event))
                    self.assertEqual(source["command_sha256"], hashlib.sha256(event["item"]["command"].encode()).hexdigest())
                    self.assertEqual(source["receipt_sha256"], caller._canonical_sha256(output["terminal_receipt"]))
                    self.assertEqual(source["receipt_path"], output["terminal_receipt_path"])
                    self.assertEqual(source["verification_evidence_sha256"], caller._canonical_sha256(output["verification_evidence"]))
                    for key in ("complete_file_coverage", "dependency_coverage", "live_source_freshness",
                                "read_permission", "parent_acceptance"):
                        self.assertIs(context[key], False)
                    self.assertEqual(evidence["mission_acceptance"], "parent_owned")

    def test_verifier_context_preimages_bind_available_snapshot_without_requiring_source_reads(self):
        for case in ("uppercase_pin", "observed_preimage"):
            def mutate(state):
                generation = state["contract"]["dcf_generation"]
                if case == "uppercase_pin":
                    pin = generation["source_snapshot"]["src/file.py"]
                    pin["sha256"] = pin["sha256"].upper()
                else:
                    generation.pop("source_snapshot")
            with self.subTest(case=case):
                terminal, _, _ = self.verifier_context_fixture(mutate)
                evidence = self.project_capability(terminal)["result_inspection"]
                self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
                self.assertIn("parent_review_context", evidence)

    def test_verifier_context_requires_binding_scopes_postimages_preimages_and_success_proof(self):
        cases = ("binding", "path", "postimage", "preimage", "snapshot", "proof_binding", "proof_postimage",
                 "proof_missing", "proof_call", "proof_verifier", "command", "executor", "origin",
                 "unadmitted", "directory", "glob", "unknown_schema", "oversized")
        for case in cases:
            def mutate(state):
                context, payload = state["context"], state["payload"]
                row = context["files"][0]
                proof = payload["verification_evidence"]
                if case == "binding":
                    context["jspace_semantic_sha256"] = "b" * 64
                elif case == "path":
                    row["path"] = "src/foreign.py"
                elif case in ("postimage", "preimage"):
                    row[case + "_sha256"] = "c" * 64
                elif case == "snapshot":
                    state["contract"]["dcf_generation"]["source_snapshot"] = None
                elif case == "proof_binding":
                    proof["authorization_semantic_sha256"] = "b" * 64
                elif case == "proof_postimage":
                    proof["source_postimages"][row["path"]]["sha256"] = "b" * 64
                elif case == "proof_missing":
                    payload.pop("verification_evidence")
                elif case == "proof_call":
                    proof["call_id"] = "foreign"
                elif case == "proof_verifier":
                    proof["verifier_sha256"] = "b" * 64
                elif case == "command":
                    state["verifier"]["command"] = '{"verifier_index":1}'
                elif case == "executor":
                    payload["executor"] = "shell"
                elif case == "origin":
                    payload["terminal_receipt"]["termination_origin"] = "shell"
                elif case in ("unadmitted", "directory", "glob"):
                    state["contract"]["read_scopes"] = [{"unadmitted": "src/foreign.py", "directory": "src",
                                                         "glob": "src/*.py"}[case]]
                elif case == "unknown_schema":
                    context["schema_version"] = "foreign_schema"
                else:
                    row["hunks"][0]["after_text"] = "x" * postimage_context.MAX_JSON_BYTES + "\npass\n"
            with self.subTest(case=case):
                terminal, _, _ = self.verifier_context_fixture(mutate)
                evidence = self.project_capability(terminal)["result_inspection"]
                self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
                self.assertEqual(evidence["command_success"], "known_success")
                self.assertNotIn("parent_review_context", evidence)

    def test_verifier_context_malformed_spans_hunks_and_locators_are_omitted_atomically(self):
        for schema in (postimage_context.SCHEMA_VERSION, postimage_context.DELTA_SCHEMA_VERSION,
                       postimage_context.DELTA_NAVIGATION_SCHEMA_VERSION):
            definition = schema == postimage_context.SCHEMA_VERSION
            cases = ["file_shape", "duplicate_file", "body_shape", "start", "count", "text", "overlap", "nul"]
            cases += ["complete", "locator_span"] if definition else ["coverage", "offset", "empty_hunk"]
            if schema != postimage_context.DELTA_SCHEMA_VERSION:
                cases += ["locator_shape", "locator_kind", "locator_name", "locator_duplicate", "locator_line"]
            for case in cases:
                def mutate(state):
                    row = state["context"]["files"][0]
                    bodies = row["spans" if definition else "hunks"]
                    body = bodies[0]
                    if case == "file_shape":
                        row["unexpected"] = True
                    elif case == "duplicate_file":
                        state["context"]["files"].append(deepcopy(row))
                    elif case == "body_shape":
                        body["unexpected"] = True
                    elif case == "start":
                        body["start_line" if definition else "after_start_line"] = 0
                    elif case == "count":
                        body["end_line" if definition else "after_line_count"] = True
                    elif case == "text":
                        body["text" if definition else "after_text"] = "only one line\n"
                    elif case == "overlap":
                        bodies.append(deepcopy(body))
                    elif case == "nul":
                        body["text" if definition else "after_text"] = "bad\x00\nsecond\n"
                    elif case == "complete":
                        row["locators"][0]["complete"] = False
                    elif case == "locator_span":
                        body.update(start_line=2, end_line=2, text="    return 'new café'\n")
                    elif case == "coverage":
                        row["coverage"] = "partial"
                    elif case == "offset":
                        body["before_start_line"] = 2
                    elif case == "empty_hunk":
                        body.update(before_line_count=0, after_line_count=0, before_text="", after_text="")
                    elif case == "locator_shape":
                        row["locators"][0]["unexpected"] = True
                    elif case == "locator_kind":
                        row["locators"][0]["kind"] = "foreign"
                    elif case == "locator_name":
                        row["locators"][0]["qualified_name"] = "bad..name"
                    elif case == "locator_duplicate":
                        row["locators"].append(deepcopy(row["locators"][0]))
                    else:
                        row["locators"][0]["line"] = False
                with self.subTest(schema=schema, case=case):
                    terminal, _, _ = self.verifier_context_fixture(mutate, schema=schema)
                    evidence = self.project_capability(terminal)["result_inspection"]
                    self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
                    self.assertNotIn("parent_review_context", evidence)

    def test_verifier_delta_empty_sides_and_physical_newlines(self):
        for before, after in (("", "pass\n"), ("old\n", ""), ("old\r\n", "pass\r\n"), ("old", "pass")):
            with self.subTest(before=before, after=after):
                terminal, _, _ = self.verifier_context_fixture(schema=postimage_context.DELTA_SCHEMA_VERSION,
                    contents={"src/file.py": after}, before_contents={"src/file.py": before})
                row = self.project_capability(terminal)["result_inspection"]["parent_review_context"]["verifier_contexts"][0]
                hunk = row["source_postimages"]["files"][0]["hunks"][0]
                self.assertEqual((hunk["before_text"], hunk["after_text"]), (before, after))

    def test_verifier_context_mutations_invalidate_even_same_sha_and_later_pass_restores(self):
        for case in ("pre_edit", "later_file", "later_patch", "batch_patch", "later_pass"):
            def mutate(state):
                events = state["events"]
                mutation = deepcopy(self.item)
                mutation.update(command_type="apply_patch", command="*** Begin Patch\n*** End Patch\n")
                if case == "pre_edit":
                    events.insert(0, events.pop(2))
                elif case == "later_file":
                    events.append(deepcopy(events[0]))
                    events[-1]["item"]["id"] = "later-file-mutation"
                elif case == "batch_patch":
                    events[2]["item"] = self.batch_item(state["verifier"], mutation)
                else:
                    events.append({"type": "item.completed", "item": mutation})
                    if case == "later_pass":
                        self.repeat_review_verifier(state)
            with self.subTest(case=case):
                terminal, _, _ = self.verifier_context_fixture(mutate)
                evidence = self.project_capability(terminal)["result_inspection"]
                self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
                if case == "later_pass":
                    rows = evidence["parent_review_context"]["verifier_contexts"]
                    self.assertEqual([row["source_event"]["event_index"] for row in rows], [4])
                else:
                    self.assertNotIn("parent_review_context", evidence)

    def test_verifier_context_multiple_observations_dedup_and_conflicts_with_reads(self):
        for case in ("repeat", "split_files", "duplicate_call", "conflict", "read", "read_conflict", "read_first_conflict"):
            def mutate(state):
                if case == "split_files":
                    first, second = state["context"]["files"]
                    state["context"]["files"] = [first]
                    other = self.repeat_review_verifier(state)
                    other["source_postimages"]["files"] = [second]
                    self.repeat_review_verifier(state, "review-verifier-2")
                elif case in ("repeat", "duplicate_call", "conflict"):
                    other = self.repeat_review_verifier(state, "review-verifier-0" if case == "duplicate_call" else "review-verifier-1")
                    if case == "repeat":
                        self.repeat_review_verifier(state, "review-verifier-2")
                    elif case == "conflict":
                        other["source_postimages"]["files"][0]["hunks"][0]["after_text"] = "different\nsecond\n"
                elif "conflict" in case:
                    state["events"][-1]["item"]["aggregated_output"]["stdout"] = "different\nsecond\n"
                    if case == "read_first_conflict":
                        state["events"][2:] = reversed(state["events"][2:])
            with self.subTest(case=case):
                contents = {"src/file.py": "def changed():\n    return 'new café'\n"}
                if case == "split_files":
                    contents["src/other.py"] = "def changed():\n    return 'other'\n"
                terminal, _, _ = self.verifier_context_fixture(mutate, contents=contents, include_read="read" in case)
                evidence = self.project_capability(terminal)["result_inspection"]
                self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
                if case in ("duplicate_call", "conflict", "read_conflict", "read_first_conflict"):
                    self.assertNotIn("parent_review_context", evidence)
                else:
                    context = evidence["parent_review_context"]
                    self.assertEqual(len(context["verifier_contexts"]), 2 if case == "split_files" else 1)
                    if case == "read":
                        self.assertEqual(context["ranges"][0]["text"], contents["src/file.py"])

    def test_large_verifier_delta_summary_recovery_and_budget_precedence(self):
        def repeat(state):
            self.repeat_review_verifier(state)
            self.repeat_review_verifier(state, "review-verifier-2")
        terminal, run, workspace = self.verifier_context_fixture(repeat, contents={
            "src/file.py": "def changed():\n    return '" + "x" * 21000 + "'\n"})
        full = inspector.inspect(self.root, terminal["request_id"], THREAD)
        context = full["parent_review_context"]
        self.assertGreater(len(caller._canonical_bytes(context)), inspector.MAX_REVIEW_CONTEXT_BYTES)
        self.assertEqual(len(context["verifier_contexts"]), 1)
        baseline = deepcopy(full)
        baseline.pop("parent_review_context")
        with patch.object(inspector, "MAX_PAGE_BYTES", len(caller._canonical_bytes(baseline)) + 8):
            self.assertEqual(inspector.inspect(self.root, terminal["request_id"], THREAD), baseline)
        original_file = inspector._file
        def artifacts_only(path, *args, **kwargs):
            self.assertFalse(Path(path).is_relative_to(workspace), "no mutable workspace reads")
            return original_file(path, *args, **kwargs)
        (workspace / "src/file.py").unlink()
        (workspace / "src").rmdir()
        workspace.rmdir()
        with patch.object(inspector, "_file", artifacts_only), \
                patch.object(postimage_context, "project", side_effect=AssertionError("no regeneration")), \
                patch.object(caller, "execute", side_effect=AssertionError("no replay")):
            recovered = self.project_capability(terminal)
        self.assertEqual(recovered["result_inspection"]["parent_review_context"], context)
        with patch.object(inspector, "inspect", side_effect=AssertionError("no reinspection")), \
                patch.object(full_core, "_Trajectory", side_effect=AssertionError("no trajectory pass")):
            summary = inspector.summarize_terminal(recovered, self.root, terminal["request_id"])
            reference = inspector.publish_inspection_summary(recovered, self.root, terminal["request_id"], expected_thread_id=THREAD)
            loaded = inspector.load_inspection_summary(reference, expected_request_id=terminal["request_id"],
                expected_thread_id=THREAD, terminal_reference={"path": str(run / "terminal.json"), "sha256": summary["terminal_sha256"]})
        self.assertEqual(loaded["result_inspection"]["parent_review_context"], context)
        protected = deepcopy(full)
        protected["commands"][0].update(diagnostic_excerpt="original failure", failure_resolution={"status": "resolved"})
        protected["verifier_failure_resolution"] = {"resolved_count": 1, "effective_command_success": "known_success"}
        protected["durable_recovery"] = {"command_index_complete": True, "evidence_source": "native_session"}
        with patch.object(inspector, "inspect", side_effect=lambda *a, **k: deepcopy(protected)):
            projected = self.project_capability(terminal)
            baseline = deepcopy(projected)
            baseline["result_inspection"].pop("parent_review_context")
            metadata = {"inspection_summary": None, "inspection_summary_omission": "READ_ONLY_RECOVERY"}
            limit = len(caller._canonical_bytes({**baseline, **metadata})) + 8
            with patch.object(inspector, "MAX_PROJECTED_BYTES", limit):
                self.assertEqual(self.project_capability(terminal), baseline)

    def test_verifier_context_combined_body_budget_omits_all_changes_not_a_prefix(self):
        def separate(state):
            first, second = state["context"]["files"]
            state["context"]["files"] = [first]
            other = self.repeat_review_verifier(state)
            other["source_postimages"]["files"] = [second]
        contents = {path: "def changed():\n    return '" + "x" * 17000 + "'\n"
                    for path in ("src/file.py", "src/other.py")}
        terminal, _, _ = self.verifier_context_fixture(separate, contents=contents)
        evidence = self.project_capability(terminal)["result_inspection"]
        self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(evidence["command_success"], "known_success")
        self.assertNotIn("parent_review_context", evidence)
        self.assertEqual(evidence["file_changes"]["targets"], 2)

    def test_verifier_context_retains_original_failure_and_verified_resolution_in_summary(self):
        def failed_first(state):
            failed = deepcopy(state["verifier"])
            failed.update(status="failed", exit_code=1)
            output = failed["aggregated_output"]
            output.update(success=False, exit_code=1, stdout="", stderr="original assertion failed")
            output.pop("source_postimages")
            output.pop("verification_evidence")
            output["terminal_receipt"].update(exit_code=1, terminal_state="failed",
                                              call_id="review-verifier-failed", failure_class="workload")
            state["events"].insert(0, {"type": "item.completed", "item": failed})
        terminal, _, _ = self.verifier_context_fixture(failed_first)
        projected = self.project_capability(terminal)
        evidence = projected["result_inspection"]
        self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(evidence["command_success"], "known_failure")
        self.assertEqual(evidence["verifier_failure_resolution"]["effective_command_success"], "known_success")
        self.assertEqual(evidence["commands"][0]["diagnostic_excerpt"], "original assertion failed")
        self.assertEqual(evidence["commands"][0]["failure_resolution"]["status"], "resolved")
        self.assertIn("parent_review_context", evidence)
        summary = inspector.summarize_terminal(projected, self.root, terminal["request_id"])
        self.assertEqual(summary["result_inspection"], evidence)

    def test_parent_context_single_batch_native_and_multiple_files_reuse_the_checked_pass(self):
        def native(events, original, contract):
            events[2]["item"].pop("command_type")
            events[2]["item"]["command"] = "source_read"
        def mixed(events, original, contract):
            status = deepcopy(self.item)
            status.update(command_type="task_status", command="{}")
            events[2]["item"] = self.batch_item(events[2]["item"], status)
        for batched, multiple, mutate in ((False, False, None), (True, False, None),
                                          (False, False, native), (False, False, mixed),
                                          (False, True, None), (True, True, None)):
            with self.subTest(batched=batched, multiple=multiple, native=mutate is not None):
                contents = {"src/file.py": "new café\nsecond\n"}
                if multiple:
                    contents["src/二.py"] = "κώδικας\n"
                terminal, run, _ = self.parent_context_fixture(mutate, contents=contents, batched=batched)
                before = {p: p.read_bytes() for p in run.iterdir() if p.is_file()}
                iterations = []
                original_iter = full_core._Trajectory.__iter__
                def counted(trajectory):
                    iterations.append(trajectory)
                    return original_iter(trajectory)
                with patch.object(full_core._Trajectory, "__iter__", counted), \
                        patch.object(inspector, "inspect", wraps=inspector.inspect) as inspected, \
                        patch.object(inspector, "_artifact", wraps=inspector._artifact) as artifacts, \
                        patch.object(file_change_evidence, "_postimage", side_effect=AssertionError("no source reread")), \
                        patch.object(caller, "execute", side_effect=AssertionError("no execution")):
                    projected = self.project_capability(terminal)
                inspected.assert_called_once()
                self.assertEqual(len(iterations), 3)  # Existing indexing, file proof and command passes only.
                self.assertEqual(sum(c.args[1] == "file-change-evidence.json" for c in artifacts.call_args_list), 1)
                evidence = projected["result_inspection"]
                self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
                self.assertEqual(evidence["mission_acceptance"], "parent_owned")
                context = evidence["parent_review_context"]
                self.assertEqual(context["coverage"], "returned_physical_lines_only")
                self.assertEqual(context["omission"], "all_unlisted_lines_files_and_dependencies")
                for key in ("complete_file_coverage", "dependency_coverage", "live_source_freshness",
                            "read_permission", "parent_acceptance"):
                    self.assertIs(context[key], False)
                self.assertLessEqual(len(caller._canonical_bytes(context)), inspector.MAX_REVIEW_CONTEXT_BYTES)
                self.assertEqual({r["path"]: r["text"] for r in context["ranges"]}, contents)
                events = [json.loads(line) for line in (run / "core.jsonl").read_text().splitlines()]
                for row in context["ranges"]:
                    self.assertEqual(row["source_sha256"], hashlib.sha256(contents[row["path"]].encode()).hexdigest())
                    self.assertEqual((row["start_line"], row["end_line"]),
                                     (1, contents[row["path"]].count("\n")))
                    source = row["source_event"]
                    self.assertEqual(source["event_sha256"], caller._canonical_sha256(events[source["event_index"]]))
                    self.assertEqual(source["part_index"], list(contents).index(row["path"]) if batched else 0)
                self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_parent_context_partial_overlaps_nested_repeats_and_novel_segments(self):
        lines = [f"line {number}\n" for number in range(1, 301)]
        cases = (
            ("partial", ((100, 199), (150, 249)), ((100, 199, 2), (200, 249, 3))),
            ("nested_and_split", ((104, 106), (110, 112), (105, 105), (103, 113), (104, 111)),
             ((104, 106, 2), (110, 112, 3), (103, 103, 5), (107, 109, 5), (113, 113, 5))),
        )
        for mode in ("assistant_reply", "evidence_only"):
            for name, bounds, expected in cases:
                with self.subTest(mode=mode, case=name):
                    def reads(events, original, contract):
                        template = deepcopy(events[2])
                        events[2:] = []
                        for start, end in bounds:
                            event = deepcopy(template)
                            event["item"]["command"] = json.dumps({"path": "src/file.py", "start_line": start, "end_line": end})
                            event["item"]["aggregated_output"].update(
                                start_line=start, end_line=end, stdout="".join(lines[start - 1:end]),
                                ends_with_newline=True, at_eof=False, next_line=end + 1)
                            events.append(event)
                    terminal, run, _ = self.parent_context_fixture(
                        reads, contents={"src/file.py": "".join(lines)}, mode=mode)
                    evidence = self.project_capability(terminal)["result_inspection"]
                    self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
                    if mode == "assistant_reply":
                        self.assertNotIn("parent_review_context", evidence)
                        continue
                    ranges = evidence["parent_review_context"]["ranges"]
                    self.assertEqual(
                        [(row["start_line"], row["end_line"], row["text"]) for row in ranges],
                        [(start, end, "".join(lines[start - 1:end])) for start, end, _ in expected])
                    observed = [number for row in ranges
                                for number in range(row["start_line"], row["end_line"] + 1)]
                    expected_lines = sorted({number for start, end in bounds
                                             for number in range(start, end + 1)})
                    self.assertEqual(sorted(observed), expected_lines)
                    events = [json.loads(line) for line in (run / "core.jsonl").read_text().splitlines()]
                    for row, (_, _, index) in zip(ranges, expected):
                        self.assertEqual(row["source_event"],
                                         {"event_index": index, "event_sha256": caller._canonical_sha256(events[index]),
                                          "part_index": 0})

    def test_parent_context_overlaps_retain_unique_content_at_byte_budget(self):
        lines = [f"{number:03d} " + "x" * 65 + "\n" for number in range(1, 301)]
        def reads(events, original, contract):
            template = deepcopy(events[2])
            events[2:] = []
            for start, end in ((100, 199), (150, 249)):
                event = deepcopy(template)
                event["item"]["command"] = json.dumps({"path": "src/file.py", "start_line": start, "end_line": end})
                event["item"]["aggregated_output"].update(
                    start_line=start, end_line=end, stdout="".join(lines[start - 1:end]),
                    ends_with_newline=True, at_eof=False, next_line=end + 1)
                events.append(event)
        for mode in ("assistant_reply", "evidence_only"):
            with self.subTest(mode=mode):
                terminal, _, _ = self.parent_context_fixture(
                    reads, contents={"src/file.py": "".join(lines)}, mode=mode)
                full = inspector.inspect(self.root, terminal["request_id"], THREAD)
                self.assertEqual(full["status"], "EVIDENCE_VERIFIED")
                if mode == "assistant_reply":
                    self.assertNotIn("parent_review_context", full)
                    continue
                context = full["parent_review_context"]
                self.assertEqual([(row["start_line"], row["end_line"]) for row in context["ranges"]],
                                 [(100, 199), (200, 249)])
                size = len(caller._canonical_bytes(context))
                self.assertLessEqual(size, inspector.MAX_REVIEW_CONTEXT_BYTES)
                duplicate = deepcopy(context["ranges"][1])
                duplicate.update(start_line=150, text="".join(lines[149:249]))
                self.assertGreater(
                    sum(len(caller._canonical_bytes(row)) for row in (context["ranges"][0], duplicate)),
                    inspector.MAX_REVIEW_CONTEXT_BYTES)
                with patch.object(inspector, "MAX_REVIEW_CONTEXT_BYTES", size):
                    self.assertEqual(inspector.inspect(self.root, terminal["request_id"], THREAD), full)
                baseline = deepcopy(full)
                baseline.pop("parent_review_context")
                with patch.object(inspector, "MAX_REVIEW_CONTEXT_BYTES", size - 1):
                    self.assertEqual(inspector.inspect(self.root, terminal["request_id"], THREAD), baseline)

    def test_parent_context_preserves_search_gaps_unterminated_utf8_and_deduplicates_ranges(self):
        def search(events, original, contract):
            item = events[2]["item"]
            item["command"] = json.dumps({"path": "src/file.py", "search_terms": ["α", "β", "尾"]})
            item["aggregated_output"].update(stdout="1: α\n3: β\n5: 尾", line_numbers=True,
                                              search_matches=[1, 3, 5])
            events.append(deepcopy(events[2]))
        terminal, _, _ = self.parent_context_fixture(search, contents={"src/file.py": "α\nskip\nβ\nskip\n尾"})
        context = self.project_capability(terminal)["result_inspection"]["parent_review_context"]
        self.assertEqual([(r["start_line"], r["end_line"], r["text"]) for r in context["ranges"]],
                         [(1, 1, "α\n"), (3, 3, "β\n"), (5, 5, "尾")])
        self.assertTrue(all(r["source_event"]["event_index"] == 2 for r in context["ranges"]))

        def partial(events, original, contract):
            item = events[2]["item"]
            item["command"] = json.dumps({"path": "src/file.py", "start_line": 2, "end_line": 3})
            item["aggregated_output"].update(start_line=2, end_line=3, stdout="2: skip\n3: β\n",
                                              line_numbers=True, ends_with_newline=True, at_eof=False, next_line=4)
        terminal, _, _ = self.parent_context_fixture(partial, contents={"src/file.py": "α\nskip\nβ\nskip\n尾"})
        ranges = self.project_capability(terminal)["result_inspection"]["parent_review_context"]["ranges"]
        self.assertEqual([(r["start_line"], r["end_line"], r["text"]) for r in ranges], [(2, 3, "skip\nβ\n")])

    def test_parent_context_missing_old_foreign_spoofed_conflicting_and_pre_edit_reads_are_omitted(self):
        cases = ("missing", "old_sha", "foreign", "wrong_input", "spoofed_type", "spoofed_origin",
                 "missing_origin", "malformed", "pre_edit", "later_patch", "batch_patch", "denied",
                 "unadmitted", "directory_scope", "glob_scope", "conflict", "inconsistent_totals",
                 "source_read_missing", "source_read_false", "command_missing", "command_denied")
        for case in cases:
            def mutate(events, original, contract):
                item = events[2]["item"]
                output = item["aggregated_output"]
                if case == "missing":
                    events.pop(2)
                elif case == "old_sha":
                    output["source_sha256"] = hashlib.sha256(b"old\n").hexdigest()
                elif case == "foreign":
                    output["path"] = "src/foreign.py"
                    item["command"] = json.dumps({"path": output["path"], "start_line": 1, "end_line": 2})
                elif case == "wrong_input":
                    item["command"] = json.dumps({"path": "src/wrong.py", "start_line": 1, "end_line": 2})
                elif case == "spoofed_type":
                    item.update(command_type="shell", command="echo source_read")
                elif case in ("spoofed_origin", "missing_origin"):
                    output["terminal_receipt"].pop("termination_origin")
                    if case == "spoofed_origin":
                        output["terminal_receipt"]["termination_origin"] = "shell"
                elif case == "malformed":
                    output.update(line_numbers=True, stdout="2: second\n1: new café\n")
                elif case == "pre_edit":
                    events.insert(0, events.pop(2))
                elif case in ("later_patch", "batch_patch"):
                    mutation = deepcopy(self.item)
                    mutation.update(command_type="apply_patch", command="*** Begin Patch\n*** End Patch\n")
                    if case == "later_patch":
                        events.append({"type": "item.completed", "item": mutation})
                    else:
                        events[2]["item"] = self.batch_item(item, mutation)
                elif case == "denied":
                    contract["denied_operations"] = ["read"]
                elif case == "source_read_missing":
                    contract.pop("source_read")
                elif case == "source_read_false":
                    contract["source_read"] = False
                elif case == "command_missing":
                    contract["allowed_operations"].remove("command")
                elif case == "command_denied":
                    contract["denied_operations"] = ["command"]
                elif case in ("unadmitted", "directory_scope", "glob_scope"):
                    contract["read_scopes"] = [{"unadmitted": "src/foreign.py", "directory_scope": "src",
                                                "glob_scope": "src/*.py"}[case]]
                else:
                    other = deepcopy(events[2])
                    other_output = other["item"]["aggregated_output"]
                    if case == "conflict":
                        other_output["stdout"] = "different\nsecond\n"
                    else:
                        other_output.update(total_lines=3, at_eof=False, next_line=3)
                    events.append(other)
            with self.subTest(case=case):
                terminal, _, _ = self.parent_context_fixture(mutate)
                evidence = self.project_capability(terminal)["result_inspection"]
                self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
                self.assertEqual(evidence["command_success"], "known_success")
                self.assertNotIn("parent_review_context", evidence)

    def test_parent_context_batch_parts_must_have_matching_types_paths_sha_and_success_proof(self):
        for case in ("old_sha", "foreign", "type", "origin", "conflict", "missing_receipt", "failed"):
            def mutate(events, original, contract):
                item = events[2]["item"]
                entry = item["aggregated_output"]["results"][0]
                output = entry["output"]
                if case == "old_sha":
                    output["source_sha256"] = "0" * 64
                elif case == "foreign":
                    output["path"] = "src/foreign.py"
                elif case == "type":
                    entry["command_type"] = "shell"
                elif case == "origin":
                    output["terminal_receipt"]["termination_origin"] = "shell"
                elif case == "conflict":
                    other = deepcopy(entry)
                    other["output"]["stdout"] = "different\nsecond\n"
                    item["aggregated_output"]["results"].append(other)
                    declared = json.loads(item["command"])
                    declared["commands"].append(deepcopy(declared["commands"][0]))
                    item["command"] = json.dumps(declared)
                elif case == "missing_receipt":
                    output.pop("terminal_receipt")
                    output.pop("terminal_receipt_path")
                else:
                    entry["success"] = False
            with self.subTest(case=case):
                terminal, _, _ = self.parent_context_fixture(mutate, batched=True)
                evidence = self.project_capability(terminal)["result_inspection"]
                self.assertNotIn("parent_review_context", evidence)
                self.assertEqual(evidence["status"], "INCOMPLETE_EVIDENCE" if case in (
                    "missing_receipt", "failed") else "EVIDENCE_VERIFIED")

    def test_parent_context_skipped_reads_leave_explicit_partial_multiple_file_coverage(self):
        def old_second(events, original, contract):
            events[3]["item"]["aggregated_output"]["source_sha256"] = "0" * 64
        terminal, _, _ = self.parent_context_fixture(old_second, contents={
            "src/file.py": "new café\nsecond\n", "src/other.py": "UNCOVERED_BODY\n"})
        projected = self.project_capability(terminal)
        evidence = projected["result_inspection"]
        self.assertEqual(evidence["file_changes"]["targets"], 2)
        context = evidence["parent_review_context"]
        self.assertEqual([r["path"] for r in context["ranges"]], ["src/file.py"])
        self.assertIs(context["complete_file_coverage"], False)
        self.assertNotIn("UNCOVERED_BODY", json.dumps(projected))

    def test_parent_context_failed_incomplete_invalid_and_foreign_evidence_cannot_deliver_source(self):
        def failed(events, original, contract):
            item = events[2]["item"]
            item.update(exit_code=7, status="failed")
            item["aggregated_output"].update(exit_code=7, terminal_receipt=dict(
                self.receipt, exit_code=7, terminal_state="failed", termination_origin="in_process_source_read"))
        def unknown(events, original, contract):
            output = events[2]["item"]["aggregated_output"]
            output.pop("terminal_receipt")
            output.pop("terminal_receipt_path")
        for mutate, blocker in ((failed, "COMMAND_FAILURE"), (unknown, "EXECUTION_PROOF_UNAVAILABLE")):
            terminal, _, _ = self.parent_context_fixture(mutate)
            evidence = self.project_capability(terminal)["result_inspection"]
            self.assertEqual(evidence["first_blocker"], blocker)
            self.assertNotIn("parent_review_context", evidence)
        with patch.object(full_core, "MAX_COMMAND_EVIDENCE", 1):
            terminal, _, _ = self.parent_context_fixture()
            evidence = self.project_capability(terminal)["result_inspection"]
        self.assertEqual(evidence["first_blocker"], "EVIDENCE_INDEX_PARTIAL")
        self.assertNotIn("parent_review_context", evidence)
        for case in ("request", "file_proof", "file_artifact", "receipt"):
            with self.subTest(case=case):
                terminal, run, _ = self.parent_context_fixture()
                if case == "request":
                    path = run / "request-identity.json"
                    original = json.loads(path.read_text())
                    original["native_thread_id"] = "87654321-1234-1234-1234-123456789abc"
                    store(path, original)
                elif case == "receipt":
                    events = [json.loads(line) for line in (run / "core.jsonl").read_text().splitlines()]
                    path = Path(events[2]["item"]["aggregated_output"]["terminal_receipt_path"])
                    store(path, dict(self.receipt, call_id="foreign-receipt"))
                else:
                    if case == "file_proof":
                        path = run / "file-change-evidence.json"
                        proof = json.loads(path.read_text())
                        proof["request_id"] = RID
                        store(path, proof)
                        terminal["file_change_evidence_artifact"] = ref(path)
                    else:
                        terminal["file_change_evidence_artifact"]["sha256"] = "0" * 64
                    store(run / "terminal.json", terminal)
                evidence = self.project_capability(terminal)["result_inspection"]
                self.assertEqual(evidence["status"], "INCOMPLETE_EVIDENCE")
                self.assertNotIn("parent_review_context", evidence)

    def test_parent_context_default_assistant_reply_read_only_and_foreign_projection_have_no_source_body(self):
        for mode, changed in ((None, True), ("assistant_reply", True), ("evidence_only", False)):
            terminal, _, _ = self.parent_context_fixture(mode=mode, changed=changed)
            with patch.object(inspector, "_ParentReviewContext", side_effect=AssertionError("no collector")):
                projected = self.project_capability(terminal)
            self.assertEqual(projected["result_inspection"]["status"], "EVIDENCE_VERIFIED")
            self.assertNotIn("parent_review_context", projected["result_inspection"])
            self.assertNotIn("new café", json.dumps(projected, ensure_ascii=False))
        terminal, _, _ = self.parent_context_fixture()
        foreign = {**terminal, "request_sha256": "0" * 64}
        self.assertNotIn("parent_review_context", self.project_capability(foreign)["result_inspection"])
        self.assertNotIn("parent_review_context", self.project_capability(
            terminal, thread="87654321-1234-1234-1234-123456789abc")["result_inspection"])

    def test_parent_context_utf8_budget_omits_whole_context_without_changing_success(self):
        terminal, _, _ = self.parent_context_fixture(contents={"src/file.py": "界" * 1000 + "\n"})
        full = inspector.inspect(self.root, terminal["request_id"], THREAD)
        context = full["parent_review_context"]
        self.assertEqual(context["ranges"][0]["text"].encode(), ("界" * 1000 + "\n").encode())
        size = len(caller._canonical_bytes(context))
        baseline = deepcopy(full)
        baseline.pop("parent_review_context")
        with patch.object(inspector, "MAX_REVIEW_CONTEXT_BYTES", size):
            self.assertEqual(inspector.inspect(self.root, terminal["request_id"], THREAD), full)
        with patch.object(inspector, "MAX_REVIEW_CONTEXT_BYTES", size - 1):
            self.assertEqual(inspector.inspect(self.root, terminal["request_id"], THREAD), baseline)
        terminal, _, _ = self.parent_context_fixture(contents={"src/file.py": "界" * 5000 + "\n"})
        projected = self.project_capability(terminal)
        self.assertEqual(projected["result_inspection"]["status"], "EVIDENCE_VERIFIED")
        self.assertNotIn("parent_review_context", projected["result_inspection"])
        self.assertLessEqual(len(caller._canonical_bytes(projected)), inspector.MAX_PROJECTED_BYTES)

    def test_parent_context_is_removed_first_at_inspection_and_terminal_budgets(self):
        terminal, _, _ = self.parent_context_fixture()
        gap = inspector._gap_diagnostic("SYNTHETIC_GAP")
        with patch.object(inspector, "_capability_gap", return_value=gap):
            full = inspector.inspect(self.root, terminal["request_id"], THREAD)
            baseline = deepcopy(full)
            baseline.pop("parent_review_context")
            limit = len(caller._canonical_bytes(baseline)) + 8
            with patch.object(inspector, "MAX_PAGE_BYTES", limit):
                bounded = inspector.inspect(self.root, terminal["request_id"], THREAD)
            self.assertEqual(bounded, baseline)
            self.assertLessEqual(len(caller._canonical_bytes(bounded)), limit)
            for bound_request in (False, True):
                projected = self.project_capability(terminal, require_request_binding=bound_request,
                                                    request_thread_id=THREAD if bound_request else None)
                baseline = deepcopy(projected)
                baseline["result_inspection"].pop("parent_review_context")
                metadata = {"inspection_summary": None, "inspection_summary_omission": "READ_ONLY_RECOVERY"}
                if bound_request:
                    metadata = {"inspection_summary": {
                        "path": str(self.root / terminal["request_id"] / inspector.INSPECTION_SUMMARY_FILE),
                        "sha256": "0" * 64, "bytes": inspector.MAX_PROJECTED_BYTES},
                        "inspection_summary_diagnostic": "x" * 64}
                limit = len(caller._canonical_bytes({**baseline, **metadata})) + 8
                with patch.object(inspector, "MAX_PROJECTED_BYTES", limit):
                    bounded = self.project_capability(terminal, require_request_binding=bound_request,
                                                      request_thread_id=THREAD if bound_request else None)
                self.assertEqual(bounded, baseline)
                self.assertLessEqual(len(caller._canonical_bytes({**bounded, **metadata})), limit)

    def test_parent_context_projection_pressure_preserves_failure_resolution_and_recovery_details(self):
        terminal, _, _ = self.parent_context_fixture()
        protected = inspector.inspect(self.root, terminal["request_id"], THREAD)
        # Exercise the projection budget in isolation with already-inspected protected fields.
        protected["commands"][0].update(execution_proof="known_failure", diagnostic_excerpt="original failure",
                                         failure_resolution={"status": "resolved", "verifiers": []})
        protected["command_success"] = "known_failure"
        protected["verifier_failure_resolution"] = {"resolved_count": 1, "unresolved_count": 0,
                                                    "effective_command_success": "known_success"}
        protected["durable_recovery"] = {"command_index_complete": True, "evidence_source": "native_session"}
        protected["capability_gap"] = inspector._gap_diagnostic("SYNTHETIC_GAP")
        with patch.object(inspector, "inspect", side_effect=lambda *a, **k: deepcopy(protected)):
            full = self.project_capability(terminal)
            baseline = deepcopy(full)
            baseline["result_inspection"].pop("parent_review_context")
            metadata = {"inspection_summary": None, "inspection_summary_omission": "READ_ONLY_RECOVERY"}
            limit = len(caller._canonical_bytes({**baseline, **metadata})) + 8
            with patch.object(inspector, "MAX_PROJECTED_BYTES", limit):
                bounded = self.project_capability(terminal)
        self.assertEqual(bounded, baseline)
        self.assertEqual(bounded["result_inspection"]["commands"][0]["diagnostic_excerpt"], "original failure")
        self.assertEqual(bounded["result_inspection"]["durable_recovery"], protected["durable_recovery"])
        self.assertIn("capability_gap", bounded)

    def test_parent_context_offline_recovery_summary_save_load_survive_workspace_changes_and_deletion(self):
        terminal, run, workspace = self.parent_context_fixture()
        original_file = inspector._file
        def artifacts_only(path, *args, **kwargs):
            self.assertFalse(Path(path).is_relative_to(workspace), "no mutable workspace reads")
            return original_file(path, *args, **kwargs)
        with patch.object(inspector, "_file", artifacts_only), \
                patch.object(file_change_evidence, "_postimage", side_effect=AssertionError("no source reread")), \
                patch.object(caller, "execute", side_effect=AssertionError("no replay")):
            projected = self.project_capability(terminal)
            context = deepcopy(projected["result_inspection"]["parent_review_context"])
            (workspace / "src/file.py").write_bytes(b"later parent changes\n")
            self.assertEqual(self.project_capability(terminal)["result_inspection"]["parent_review_context"], context)
            (workspace / "src/file.py").unlink()
            (workspace / "src").rmdir()
            workspace.rmdir()
            recovered = self.project_capability(terminal)
            self.assertEqual(recovered["result_inspection"]["parent_review_context"], context)
            with patch.object(inspector, "inspect", side_effect=AssertionError("no reinspection")), \
                    patch.object(full_core, "_Trajectory", side_effect=AssertionError("no trajectory pass")):
                summary = inspector.summarize_terminal(recovered, self.root, terminal["request_id"])
                reference = inspector.publish_inspection_summary(recovered, self.root, terminal["request_id"],
                                                                  expected_thread_id=THREAD)
                loaded = inspector.load_inspection_summary(reference, expected_request_id=terminal["request_id"],
                    expected_thread_id=THREAD, terminal_reference={"path": str(run / "terminal.json"),
                                                                  "sha256": summary["terminal_sha256"]})
            self.assertEqual(loaded, summary)
            self.assertEqual(loaded["result_inspection"]["parent_review_context"], context)

    def test_capability_gap_absence_is_a_no_op(self):
        before = deepcopy(self.terminal)
        projected = self.project_capability(self.terminal)
        self.assertNotIn("capability_gap", projected)
        self.assertEqual(self.terminal, before)
        self.assertEqual(projected["result_inspection"]["status"], "EVIDENCE_VERIFIED")

    def test_capability_gap_is_bound_diagnostic_not_authorization_or_execution(self):
        data = self.capability_handoff()
        terminal, run, _ = self.capability_fixture(self.capability_text(data))
        before = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
        with patch.object(caller, "execute", side_effect=AssertionError("no execution")):
            projected = self.project_capability(terminal, request_thread_id=THREAD, require_request_binding=True)
        gap = projected["capability_gap"]
        self.assertEqual(gap["status"], "MODEL_REPORTED")
        self.assertEqual(gap["handoff"], data)
        self.assertEqual(gap["binding"]["request_id"], terminal["request_id"])
        self.assertEqual(gap["binding"]["native_thread_id"], THREAD)
        self.assertEqual(gap["binding"]["terminal_sha256"], hashlib.sha256(before[run / "terminal.json"]).hexdigest())
        self.assertTrue(gap["requires_parent_readmission"])
        self.assertFalse(gap["permission_grant"])
        self.assertFalse(gap["retry_safe"])
        self.assertEqual(gap["authority_effect"], "none")
        self.assertEqual(gap["execution_proof"], "unproven")
        self.assertEqual(gap["mission_acceptance"], "parent_owned")
        self.assertEqual(projected["result_inspection"]["first_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
        self.assertEqual({key: projected[key] for key in terminal}, terminal)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertNotIn("capability_gap", caller.read_terminal(self.root, terminal["request_id"]))
        with patch.object(inspector, "inspect", side_effect=AssertionError("no second inspection")):
            summary = inspector.summarize_terminal(projected, self.root, terminal["request_id"])
        self.assertEqual(summary["capability_gap"], gap)

    def test_capability_gap_rejects_malformed_duplicate_and_oversized_json(self):
        valid = json.dumps(self.capability_handoff())
        cases = ["{", "[]", valid.replace('"schema_version":', '"schema_version":"duplicate","schema_version":', 1),
                 valid.replace('"tool":', '"tool":"duplicate","tool":', 1),
                 valid + " " * inspector.MAX_CAPABILITY_GAP_BYTES]
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                terminal, _, _ = self.capability_fixture(self.capability_text(raw))
                gap = self.project_capability(terminal)["capability_gap"]
                self.assertEqual(gap["status"], "UNAVAILABLE")
                expected = ("CAPABILITY_GAP_TOO_LARGE" if len(raw.encode()) > inspector.MAX_CAPABILITY_GAP_BYTES
                            else "CAPABILITY_GAP_INVALID_JSON")
                self.assertEqual(gap["first_blocker"], expected)
                self.assertNotIn("handoff", gap)
        text = self.capability_text(valid)
        for invalid in (text + text, text.replace("\n```nokiy", " inline ```nokiy"), text.removesuffix("```\n")):
            terminal, _, _ = self.capability_fixture(invalid)
            self.assertEqual(self.project_capability(terminal)["capability_gap"]["status"], "UNAVAILABLE")

    def test_capability_gap_rejects_unknown_keys_wrong_types_and_duplicate_capabilities(self):
        data = self.capability_handoff()
        cases = [dict(data, extra=True), dict(data, schema_version="other"), dict(data, completed_work=True),
                 dict(data, remaining_work=[]), dict(data, completed_work=["x" * 513]),
                 dict(data, unified_diff=False), dict(data, unified_diff=None), dict(data, missing_capabilities=[]),
                 dict(data, missing_capabilities=data["missing_capabilities"] * 2),
                 dict(data, missing_capabilities=[dict(data["missing_capabilities"][0], extra=True)]),
                 dict(data, missing_capabilities=[{"path": None, "operation": True, "tool": "run"}]),
                 dict(data, missing_capabilities=[{"path": None, "operation": "command", "tool": "python check.py"}])]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                terminal, _, _ = self.capability_fixture(self.capability_text(invalid))
                gap = self.project_capability(terminal)["capability_gap"]
                self.assertEqual(gap["status"], "UNAVAILABLE")
                self.assertNotIn("handoff", gap)

    def test_capability_gap_rejects_outside_alias_wildcard_and_protected_paths(self):
        for path in ("/tmp/out.py", "../out.py", "src/../out.py", "./src/file.py", "src//file.py",
                     "src/*.py", "src/?.py", "src/[a].py", ".git/config", "src/.GIT/config",
                     r"src\file.py", "C:/file.py", "src/file.py\n", "", 7, None):
            with self.subTest(path=path):
                data = self.capability_handoff()
                data["missing_capabilities"][0]["path"] = path
                data["unified_diff"] = None
                terminal, _, _ = self.capability_fixture(self.capability_text(data))
                self.assertEqual(self.project_capability(terminal)["capability_gap"]["first_blocker"],
                                 "CAPABILITY_GAP_PATH_INVALID")

    def test_capability_gap_rejects_file_and_directory_symlinks(self):
        for path in ("src/link.py", "linkdir/file.py"):
            data = self.capability_handoff()
            data["missing_capabilities"][0]["path"], data["unified_diff"] = path, None
            terminal, _, workspace = self.capability_fixture(self.capability_text(data))
            link = workspace / ("src/link.py" if path.startswith("src/") else "linkdir")
            link.symlink_to(self.root / "foreign")
            self.assertEqual(self.project_capability(terminal)["capability_gap"]["first_blocker"],
                             "CAPABILITY_GAP_PATH_UNSAFE")

    def test_capability_gap_requires_real_consistent_safe_diff_or_precise_missing_read(self):
        base = self.capability_handoff()
        for diff in ("", "patch proposed", "--- a/src/file.py\n+++ b/src/file.py\n",
                     base["unified_diff"].replace("-1 +1", "-1,2 +1"),
                     base["unified_diff"].replace("src/file.py", "../outside.py"),
                     base["unified_diff"].replace("src/file.py", "src/unlisted.py"),
                     base["unified_diff"] + "run tests\n", "x" * (inspector.MAX_CAPABILITY_DIFF_BYTES + 1)):
            with self.subTest(diff=diff[:80]):
                terminal, _, _ = self.capability_fixture(self.capability_text(dict(base, unified_diff=diff)))
                self.assertEqual(self.project_capability(terminal)["capability_gap"]["status"], "UNAVAILABLE")
        missing = dict(base, missing_capabilities=base["missing_capabilities"] +
                       [{"path": "src/missing.py", "operation": "read", "tool": "source_read"}],
                       unified_diff=None)
        terminal, _, _ = self.capability_fixture(self.capability_text(missing))
        self.assertEqual(self.project_capability(terminal)["capability_gap"]["handoff"], missing)
        pathless = dict(base, missing_capabilities=[{"path": None, "operation": "command", "tool": "read_media"}],
                        unified_diff=None)
        terminal, _, _ = self.capability_fixture(self.capability_text(pathless))
        self.assertEqual(self.project_capability(terminal)["capability_gap"]["handoff"], pathless)

    def test_capability_gap_create_delete_diff_operations_must_match_the_report(self):
        for operation, path, diff in (
            ("create", "src/new.py", "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+new\n"),
            ("delete", "src/file.py", "--- a/src/file.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"),
        ):
            with self.subTest(operation=operation):
                data = dict(self.capability_handoff(), unified_diff=diff,
                            missing_capabilities=[{"path": path, "operation": operation, "tool": "apply_patch"}])
                terminal, _, _ = self.capability_fixture(self.capability_text(data))
                self.assertEqual(self.project_capability(terminal)["capability_gap"]["handoff"], data)
                data["missing_capabilities"][0]["operation"] = "modify"
                terminal, _, _ = self.capability_fixture(self.capability_text(data))
                self.assertEqual(self.project_capability(terminal)["capability_gap"]["first_blocker"],
                                 "CAPABILITY_GAP_DIFF_INVALID")

    def test_capability_gap_cannot_borrow_request_or_caller_identity(self):
        terminal, run, _ = self.capability_fixture(self.capability_text(self.capability_handoff()))
        projected = self.project_capability(terminal, thread="87654321-1234-1234-1234-123456789abc",
                                           request_thread_id=THREAD, require_request_binding=True)
        self.assertEqual(projected["result_inspection"]["first_blocker"], "CALLER_THREAD_ID_MISMATCH")
        self.assertEqual(projected["capability_gap"]["status"], "UNAVAILABLE")
        forged = dict(terminal, result_text=terminal["result_text"] + "forged")
        self.assertEqual(self.project_capability(forged)["capability_gap"]["first_blocker"],
                         "CAPABILITY_GAP_TERMINAL_PROJECTION_MISMATCH")
        identity = json.loads((run / "request-identity.json").read_text())
        identity["native_thread_id"] = "87654321-1234-1234-1234-123456789abc"
        store(run / "request-identity.json", identity)
        projected = self.project_capability(terminal)
        self.assertEqual(projected["result_inspection"]["first_blocker"], "TRAJECTORY_REQUEST_IDENTITY_MISMATCH")
        self.assertEqual(projected["capability_gap"]["status"], "UNAVAILABLE")

    def test_capability_gap_never_parses_truncated_preview_or_bulk_reads_result(self):
        data = self.capability_handoff()
        text = self.capability_text(data) + "x" * (caller.MAX_RESULT_BYTES + 100)
        terminal, _, _ = self.capability_fixture(text)
        self.assertTrue(terminal["result_truncated"])
        decode = inspector._gap_envelope
        def complete_only(value):
            self.assertEqual(value, text)
            self.assertNotEqual(value, terminal["result_text"])
            return decode(value)
        with patch.object(inspector, "_gap_envelope", side_effect=complete_only) as parsed, \
                patch.object(Path, "read_text", side_effect=AssertionError("no bulk read")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no bulk read")):
            gap = self.project_capability(terminal)["capability_gap"]
        parsed.assert_called_once_with(text)
        self.assertEqual(gap["status"], "MODEL_REPORTED")
        self.assertEqual(gap["handoff"], data)
        self.assertTrue(gap["requires_parent_readmission"])
        self.assertFalse(gap["permission_grant"])

    def test_capability_gap_obeys_projection_budget_without_overwriting_actual_blocker(self):
        terminal, run, _ = self.capability_fixture(self.capability_text(self.capability_handoff()))
        terminal.update(status="BLOCKED", first_typed_blocker="ORIGINAL_SCOPE_DENIED")
        store(run / "terminal.json", terminal)
        projected = self.project_capability(terminal)
        baseline = {key: value for key, value in projected.items() if key != "capability_gap"}
        limit = (len(caller._canonical_bytes(baseline)) + 32 +
                 len(caller._canonical_bytes(inspector._gap_diagnostic("CAPABILITY_GAP_PROJECTION_TOO_LARGE"))))
        with patch.object(inspector, "MAX_PROJECTED_BYTES", limit):
            bounded = self.project_capability(terminal)
        self.assertLessEqual(len(caller._canonical_bytes(bounded)), limit)
        self.assertEqual(bounded["capability_gap"]["first_blocker"], "CAPABILITY_GAP_PROJECTION_TOO_LARGE")
        self.assertEqual(bounded["status"], "BLOCKED")
        self.assertEqual(bounded["first_typed_blocker"], "ORIGINAL_SCOPE_DENIED")
        self.assertEqual(bounded["result_inspection"]["first_blocker"], projected["result_inspection"]["first_blocker"])

    def source_read_item(self):
        item = deepcopy(self.item)
        item.update(command_type="source_read", command=json.dumps({
            "path": "src/example.py", "start_line": 1, "end_line": 1}))
        item["aggregated_output"].update(
            path="src/example.py", source_sha256="b" * 64, start_line=1, end_line=1,
            total_lines=1, line_numbers=True, stdout="1: SOURCE_BODY_NOT_FOR_PROJECTION\n",
            stderr="", at_eof=True, next_line=None, ends_with_newline=True,
            truncated=False, truncation_reason=None)
        return item

    def verifier_item(self, stdout="Ran 7 tests in 0.012s\n\nOK\n"):
        item = deepcopy(self.item)
        item.update(command_type="focused_verifier", command=json.dumps({"verifier_index": 0}))
        item["aggregated_output"].update(stdout=stdout, stderr="")
        return item

    def batch_item(self, *items):
        batch = deepcopy(self.item)
        batch.update(command_type="command_run", command=json.dumps({"commands": [
            {"command_type": item["command_type"], "command_line": item["command"], "step": 1}
            for item in items]}))
        batch["aggregated_output"]["results"] = [
            {"command_type": item["command_type"], "command_line": item["command"],
             "success": item.get("success", True), "output": deepcopy(item["aggregated_output"])}
            for item in items]
        return batch

    def resolution_fixture(self, mutate=None, *, targets=("src/file.py", "src/unchanged.txt"), changed=True):
        workspace = self.root / "resolution-workspace"
        (workspace / "src").mkdir(parents=True, exist_ok=True)
        for name in targets:
            (workspace / name).write_bytes(b"old\n")
        snapshot = {name: file_change_evidence._postimage(workspace, name) for name in targets}
        grant = {"argv": ["/python", "/public-test.py"], "scratch_root": "/scratch",
                 "timeout_seconds": 7, "pinned_files": [{"path": "/public-test.py", "sha256": "b" * 64}],
                 "python_import_roots": [str(workspace / "src")]}
        contract = {"repo_root": str(workspace), "authorization_semantic_sha256": "a" * 64,
                    "read_scopes": list(targets), "write_scopes": list(targets), "declared_targets": list(targets),
                    "allowed_operations": ["command", "read", "modify"], "denied_operations": [],
                    "verifier_commands": [grant], "dcf_generation": {"source_snapshot": snapshot}}
        original = {"schema_version": caller.REQUEST_SCHEMA_VERSION, "native_thread_id": THREAD,
                    "execution_profile": "direct", "model": "gpt-6.1-sol", "reasoning_effort": "max",
                    "workspace": str(workspace), "max_result_bytes": caller.MAX_RESULT_BYTES}
        def verifier(code, name, index=0):
            receipt = dict(self.receipt, exit_code=code, terminal_state="completed" if code == 0 else "failed",
                           termination_origin="parent_verifier", call_id="parent-" + name,
                           failure_class="workload")
            return {"type": "command_execution", "command_type": "focused_verifier",
                    "status": "completed" if code == 0 else "failed", "exit_code": code,
                    "command": json.dumps({"verifier_index": index}),
                    "aggregated_output": {"success": code == 0, "exit_code": code, "outcome": "known",
                                          "process_reaped": True, "process_group_empty": True,
                                          "stdout": "public-test\n", "stderr": "" if code == 0 else "assertion failed",
                                          "executor": "parent_focused_verifier", "terminal_receipt": receipt}}
        failed, passed = verifier(1, "failed"), verifier(0, "passed")
        events = [{"type": "item.completed", "item": failed}]
        if changed and targets:
            (workspace / targets[0]).write_bytes(b"new\n")
            events.append({"type": "item.completed", "item": {
                "type": "file_change", "id": "patch-0", "status": "completed",
                "changes": [{"kind": "update", "path": str(workspace / targets[0])}]}})
        passed["aggregated_output"]["verification_evidence"] = {
            "schema_version": "nokiy_focused_verifier_evidence_v1",
            "authorization_semantic_sha256": contract["authorization_semantic_sha256"],
            "verifier_index": 0, "verifier_sha256": caller._canonical_sha256(grant),
            "call_id": "parent-passed",
            "source_postimages": {name: file_change_evidence._postimage(workspace, name) for name in targets},
        }
        events.append({"type": "item.completed", "item": passed})
        state = dict(workspace=workspace, original=original, contract=contract, failed=failed,
                     passed=passed, events=events, verifier=verifier)
        if mutate is not None:
            mutate(state)
        original["jspace_contract"] = {"sha256": caller._canonical_sha256(contract)}
        digest = caller._canonical_sha256(original)
        original.update(request_id="tura_embedded_" + digest, request_sha256=digest)
        run = self.root / original["request_id"]
        run.mkdir(exist_ok=True)
        payloads = [event["item"]["aggregated_output"] for event in events
                    if event["item"].get("type") == "command_execution"]
        payloads += [entry["output"] for payload in list(payloads) for entry in payload.get("results", [])]
        for i, payload in enumerate(payloads):
            if isinstance(payload.get("terminal_receipt"), dict):
                path = run / f"execution-state/command_receipts/parent-{i}.json"
                store(path, payload["terminal_receipt"])
                payload["terminal_receipt_path"] = str(path)
        events.extend([
            {"type": "item.completed", "item": {"type": "assistant_message", "text": "repaired"}},
            {"type": "turn.completed", "status": "completed", "usage": self.terminal["usage"],
             **full_core._terminal_identity(original)},
        ])
        trajectory = run / "core.jsonl"
        trajectory.write_bytes(b"".join(caller._canonical_bytes(event) + b"\n" for event in events))
        evidence = full_core._command_evidence(events)
        store(run / "command-evidence.json", evidence)
        store(run / "supervision.json", self.supervision)
        store(run / "request-identity.json", original)
        for name in ("jspace-original.json", "jspace.json"):
            store(run / name, contract)
        terminal = {**self.terminal,
                    **{key: original[key] for key in ("request_id", "request_sha256", "model",
                                                      "reasoning_effort", "execution_profile")},
                    "execution_model": "single_task_full_core", "requested_service_tier": "default",
                    "result_text": "repaired", "result_truncated": False, "result_artifact": None,
                    "trajectory_artifact": ref(trajectory), "supervision_artifact": ref(run / "supervision.json"),
                    "command_evidence_artifact": ref(run / "command-evidence.json"),
                    "command_evidence_summary": {k: evidence[k] for k in ("total_count", "failed_count", "complete")}}
        proof = file_change_evidence.produce(events, SimpleNamespace(
            workspace=workspace, request_id=original["request_id"],
            request_sha256=original["request_sha256"], native_thread_id=THREAD), contract)
        if proof is not None:
            store(run / "file-change-evidence.json", proof)
            terminal.update(file_change_evidence_artifact=ref(run / "file-change-evidence.json"),
                            original_jspace_artifact=ref(run / "jspace-original.json"),
                            file_change_evidence_summary={k: proof[k] for k in ("total_count", "target_count")})
        store(run / "terminal.json", terminal)
        state.update(terminal=terminal, run=run)
        return state

    def inspect_resolution(self, fixture, **kwargs):
        return inspector.inspect(self.root, fixture["terminal"]["request_id"], THREAD, **kwargs)

    def source_read_rejection_fixture(self, *, line=None, command=None, changed=True,
                                      include_read=True, include_pass=True, emit_marker=True,
                                      contract_change=None):
        line = line if line is not None else '{"path":"answer.txt","start_line":1,"end_line": ninety}'
        canonical = ({"command_type": "source_read", "command_line": line} if command is None else
                     {key: value for key, value in command.items() if key in
                      {"command_type", "command_line", "step", "id", "timeout_ms", "stall_timeout_ms"}})
        canonical.setdefault("step", 1)
        rejected = {"type": "command_execution", "id": "item_1", "status": "failed", "exit_code": None,
                    "command": line if command is None else json.dumps({"commands": [command]}, ensure_ascii=False),
                    "aggregated_output": {"pre_execution_rejection": {}}}
        if command is not None:
            rejected["command_type"] = "command_run"
        def prepare(state):
            state["contract"]["source_read"] = True
            if contract_change is not None:
                contract_change(state["contract"])
            if emit_marker:
                state["original"]["terminal_delivery"] = "evidence_only"
            state["events"][0]["item"] = rejected
            if not include_pass:
                state["events"].pop()
            if include_read:
                read = self.source_read_item()
                read.pop("command_type")  # The actual runtime projects the arguments only.
                read["command"] = json.dumps({"path": "src/file.py", "start_line": 1, "end_line": 1})
                read["aggregated_output"].update(path="src/file.py", stdout="1: new\n" if changed else "1: old\n",
                    source_sha256=file_change_evidence._postimage(state["workspace"], "src/file.py")["sha256"])
                read["aggregated_output"]["terminal_receipt"].update(
                    termination_origin="in_process_source_read", call_id="fixture-runtime.tool.command_run:call_read:0")
                state["events"].insert(len(state["events"]) - int(include_pass),
                                         {"type": "item.completed", "item": read})
        fixture = self.resolution_fixture(prepare, changed=changed)
        proof = {"schema_version": "nokiy_source_read_pre_execution_rejection_v1", "owner": "router",
                 "rejection_kind": "json_syntax_or_shape", "session_id": "full-" + fixture["original"]["request_sha256"],
                 "runtime_id": "fixture-runtime", "execution_id": "fixture-runtime.tool.command_run:fixture-call:0",
                 "call_id": "fixture-runtime.tool.command_run:fixture-call:0",
                 "authorization_semantic_sha256": fixture["contract"]["authorization_semantic_sha256"],
                 "command_sha256": hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest(),
                 "step": canonical.get("step", 1), "error_message": "typed JSON arguments were rejected",
                 "effect_state": "not_started", "process_started": False, "source_content_read": False,
                 "mutation_count": 0, "authority_effect": "none"}
        fixture.update(rejected=rejected, rejection_proof=proof, canonical_command=canonical)
        if emit_marker:
            marker = {"type": "nokiy.terminal_evidence", "schema_version": "nokiy_terminal_evidence_v1",
                      "session_id": proof["session_id"], "runtime_id": "fixture-final-runtime", "terminal_status": "done",
                      "delivery_mode": "evidence_only", "parent_acceptance_required": True,
                      "final_summary_turn_executed": False}
            fixture["events"].insert(len(fixture["events"]) - 1, marker)
            fixture["terminal"].update(requested_terminal_delivery="evidence_only",
                observed_terminal_delivery="evidence_only", terminal_evidence=marker,
                result_text=caller._canonical_bytes({key: marker[key] for key in
                    ("terminal_status", "delivery_mode", "parent_acceptance_required",
                     "final_summary_turn_executed")}).decode())
        self.republish_source_read_rejection(fixture)
        return fixture

    def republish_source_read_rejection(self, fixture, *, write_witness=True):
        proof = fixture["rejection_proof"]
        fixture["rejected"]["aggregated_output"] = json.dumps(
            {"pre_execution_rejection": proof}, ensure_ascii=False, separators=(",", ":"))
        identity = [proof[key] for key in ("session_id", "runtime_id", "execution_id", "call_id")]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        fixture["rejection_witness"] = fixture["run"] / (
            "execution-state/command_receipts/source-read-preexecution-" + digest + ".json")
        if write_witness:
            store(fixture["rejection_witness"], proof)
        self.republish_terminal_evidence(fixture["terminal"], fixture["run"], fixture["events"])

    def test_pre_execution_rejection_reconciles_without_erasing_failed_history_or_replaying(self):
        fixture = self.source_read_rejection_fixture()
        self.assertEqual(fixture["rejection_proof"]["command_sha256"],
                         "bc9280e89420013017fe22950605b1a35db245145a3a4589534aa6d912bf0c76")
        before = {path: path.read_bytes() for path in fixture["run"].rglob("*") if path.is_file()}
        with patch.object(caller, "execute", side_effect=AssertionError("no replay")), \
                patch.object(file_change_evidence, "_postimage", side_effect=AssertionError("no source reread")):
            result = self.inspect_resolution(fixture)
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        self.assertIsNone(result["first_blocker"])
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertEqual(result["command_success"], "known_failure")
        row = result["commands"][0]
        self.assertEqual((row["status"], row["exit_code"], row["receipt_check"], row["execution_proof"]),
                         ("failed", None, "unavailable", "not_attempted"))
        self.assertNotIn("failure_resolution", row)
        self.assertNotIn("verifier_failure_resolution", result)
        compact = row["pre_execution_rejection"]
        self.assertEqual(compact["receipt_check"], "matched")
        self.assertEqual(compact["receipt"]["sha256"], hashlib.sha256(fixture["rejection_witness"].read_bytes()).hexdigest())
        for key in ("effect_state", "process_started", "source_content_read", "mutation_count", "authority_effect"):
            self.assertIs(type(compact[key]), type(fixture["rejection_proof"][key]))
            self.assertEqual(compact[key], fixture["rejection_proof"][key])
        self.assertEqual(result["source_read_rejection_reconciliation"], {
            "schema_version": "nokiy_source_read_rejection_reconciliation_v1", "not_attempted_count": 1,
            "unresolved_count": 0, "effective_command_success": "known_success"})
        self.assertEqual(result["source_read_efficiency"]["source_read_calls"], 1)
        self.assertEqual(result["source_read_efficiency"]["returned_lines"], 1)
        self.assertEqual(result["mission_acceptance"], "parent_owned")
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        projected = self.project_capability(fixture["terminal"])
        self.assertEqual(projected["result_inspection"]["commands"][0]["pre_execution_rejection"], compact)
        with patch.object(inspector, "inspect", side_effect=AssertionError("no second inspection")):
            summary = inspector.summarize_terminal(projected, self.root, fixture["terminal"]["request_id"])
        self.assertEqual(summary["result_inspection"], projected["result_inspection"])

    def test_pre_execution_rejection_requires_a_real_successful_action(self):
        fixture = self.source_read_rejection_fixture(changed=False, include_read=False, include_pass=False)
        result = self.inspect_resolution(fixture)
        self.assertEqual(result["first_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
        self.assertEqual(result["command_success"], "known_failure")
        self.assertEqual(result["source_read_rejection_reconciliation"]["effective_command_success"], "unproven")
        self.assertEqual(result["source_read_efficiency"]["source_read_calls"], 0)
        for changed, read, passed in ((True, False, False), (False, True, False), (False, False, True)):
            with self.subTest(changed=changed, read=read, passed=passed):
                result = self.inspect_resolution(self.source_read_rejection_fixture(
                    changed=changed, include_read=read, include_pass=passed))
                self.assertEqual(result["status"], "EVIDENCE_VERIFIED")

    def test_pre_execution_rejection_exact_metadata_alias_reporting_and_utf8_hashes(self):
        line = '{"path":"é.txt","end_line": ninety}'
        command = {"command_type": "source_read", "command": "source_read", "command_line": line,
                   "step": 7, "id": "read-é", "timeout_ms": 900, "stall_timeout_ms": 800,
                   "command_id": "report-only", "command_run_id": "run-only",
                   "provider_tool_call_id": "provider-only", "command_index": 0}
        fixture = self.source_read_rejection_fixture(command=command)
        fixture["rejection_proof"].update(call_id="fixture-runtime.tool.command_run:appel-é:0",
                                          execution_id="fixture-runtime.tool.command_run:appel-é:0")
        self.republish_source_read_rejection(fixture)
        self.assertNotEqual(fixture["rejection_proof"]["command_sha256"],
                            caller._canonical_sha256(fixture["canonical_command"]))
        result = self.inspect_resolution(fixture)
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(result["commands"][0]["pre_execution_rejection"]["step"], 7)
        for key in ("step", "id", "timeout_ms", "stall_timeout_ms"):
            with self.subTest(omitted=key):
                command = {"command_type": "source_read", "command_line": line,
                           key: "original-id" if key == "id" else 2}
                fixture = self.source_read_rejection_fixture(command=command)
                fixture["rejected"].pop("command_type")
                fixture["rejected"]["command"] = line
                self.republish_source_read_rejection(fixture)
                self.assertEqual(self.inspect_resolution(fixture)["first_blocker"], "COMMAND_FAILURE")
        self.assertEqual(self.inspect_resolution(self.source_read_rejection_fixture(emit_marker=False))["status"],
                         "EVIDENCE_VERIFIED")

    def test_pre_execution_rejection_shape_errors_but_not_semantic_failures(self):
        for line in ('{"path":"src/file.py","end_line":NaN}',
                     '{"path":"src/file.py","path":"other.py"}'):
            with self.subTest(strict_json=line):
                result = self.inspect_resolution(self.source_read_rejection_fixture(line=line))
                self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        for args in ({"path": "src/file.py", "end_line": "ninety"},
                     {"path": True, "end_line": 1}, {"path": "src/file.py", "start_line": True},
                     {"path": "src/file.py", "line_numbers": 0},
                     {"path": "src/file.py", "search_terms": [1]}):
            with self.subTest(shape=args):
                result = self.inspect_resolution(self.source_read_rejection_fixture(line=json.dumps(args)))
                self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        for args in ({"path": "src/file.py", "start_line": 0, "end_line": 999999},
                     {"path": "../private.txt", "start_line": 1, "end_line": 1},
                     {"path": "src/file.py", "expected_sha256": "not-a-valid-sha"},
                     {"path": "src/file.py", "search_terms": []},
                     {"path": "src/file.py", "start_line": 2, "end_line": 1}):
            with self.subTest(semantic=args):
                result = self.inspect_resolution(self.source_read_rejection_fixture(line=json.dumps(args)))
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertNotIn("source_read_rejection_reconciliation", result)

    def test_pre_execution_rejection_invalid_fields_never_excuse_failure_even_with_matching_witness(self):
        invalid = {"schema_version": "other", "owner": "worker", "rejection_kind": "permission_denied",
                   "session_id": "full-foreign", "runtime_id": "foreign-runtime", "execution_id": "",
                   "call_id": " ", "authorization_semantic_sha256": "b" * 64, "command_sha256": "b" * 64,
                   "step": True, "error_message": [], "effect_state": "started", "process_started": True,
                   "source_content_read": True, "mutation_count": False, "authority_effect": "write"}
        for key, value in list(invalid.items()) + [("step", 0), ("step", 2**64), ("mutation_count", 1),
                                                  ("unknown_field", "ignored?"), ("error_message", "é" * 4096)]:
            with self.subTest(key=key, value=str(value)[:40]):
                fixture = self.source_read_rejection_fixture()
                fixture["rejection_proof"][key] = value
                self.republish_source_read_rejection(fixture)
                result = self.inspect_resolution(fixture)
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertNotIn("pre_execution_rejection", result["commands"][0])
        for key in ("runtime_id", "execution_id", "call_id", "session_id", "authority_effect", "mutation_count"):
            fixture = self.source_read_rejection_fixture()
            fixture["rejected"][key] = False if key == "mutation_count" else "foreign"
            self.republish_source_read_rejection(fixture)
            with self.subTest(observation=key):
                self.assertEqual(self.inspect_resolution(fixture)["first_blocker"], "COMMAND_FAILURE")

    def test_pre_execution_rejection_does_not_expand_contract_authority(self):
        changes = (lambda c: c.update(source_read=False), lambda c: c.update(source_read=1),
                   lambda c: c["allowed_operations"].remove("read"),
                   lambda c: c["denied_operations"].append("read"), lambda c: c.update(read_scopes=[]),
                   lambda c: c.update(repo_root="/foreign-workspace"),
                   lambda c: c.update(authorization_semantic_sha256="A" * 64))
        for index, change in enumerate(changes):
            with self.subTest(authority=index):
                result = self.inspect_resolution(self.source_read_rejection_fixture(changed=False, contract_change=change))
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertNotIn("source_read_rejection_reconciliation", result)
        fixture = self.source_read_rejection_fixture()
        contract = dict(fixture["contract"], source_read=False)
        store(fixture["run"] / "jspace.json", contract)
        self.assertEqual(self.inspect_resolution(fixture)["first_blocker"], "COMMAND_FAILURE")

    def test_pre_execution_rejection_requires_request_local_strict_regular_witness(self):
        for case in ("missing", "foreign", "mutated", "bool_step", "bool_mutations", "unknown", "duplicate",
                     "invalid_utf8", "oversized", "symlink"):
            with self.subTest(witness=case):
                fixture = self.source_read_rejection_fixture()
                path, proof = fixture["rejection_witness"], fixture["rejection_proof"]
                if case in ("missing", "foreign", "symlink"):
                    path.unlink()
                    if case != "missing":
                        target = self.run / path.name if case == "foreign" else fixture["run"] / "other-proof.json"
                        store(target, proof)
                        if case == "symlink":
                            path.symlink_to(target)
                elif case in ("mutated", "bool_step", "bool_mutations", "unknown"):
                    key, value = {"mutated": ("error_message", "different"), "bool_step": ("step", True),
                                  "bool_mutations": ("mutation_count", False), "unknown": ("extra", 1)}[case]
                    store(path, dict(proof, **{key: value}))
                elif case == "duplicate":
                    path.write_bytes(caller._canonical_bytes(proof)[:-1] + b',"mutation_count":0}')
                else:
                    path.write_bytes(b"\xff" if case == "invalid_utf8" else b" " * 4097)
                result = self.inspect_resolution(fixture)
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertNotIn("source_read_rejection_reconciliation", result)
        fixture = self.source_read_rejection_fixture()
        directory = fixture["rejection_witness"].parent
        target = directory.with_name("other-receipts")
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
        self.assertNotEqual(self.inspect_resolution(fixture)["status"], "EVIDENCE_VERIFIED")

    def test_pre_execution_rejection_duplicate_or_reused_call_cannot_be_reconciled(self):
        for case in ("duplicate", "other_execution", "ordinary_receipt"):
            fixture = self.source_read_rejection_fixture()
            if case == "ordinary_receipt":
                payload = fixture["passed"]["aggregated_output"]
                payload["terminal_receipt"]["call_id"] = fixture["rejection_proof"]["call_id"]
                store(Path(payload["terminal_receipt_path"]), payload["terminal_receipt"])
            else:
                item = deepcopy(fixture["rejected"])
                if case == "other_execution":
                    proof = dict(fixture["rejection_proof"], execution_id="another-execution")
                    duplicate = dict(fixture, rejected=item, rejection_proof=proof)
                    self.republish_source_read_rejection(duplicate)
                fixture["events"].insert(len(fixture["events"]) - 3, {"type": "item.completed", "item": item})
            self.republish_terminal_evidence(fixture["terminal"], fixture["run"], fixture["events"])
            with self.subTest(reused=case):
                result = self.inspect_resolution(fixture)
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertNotIn("source_read_rejection_reconciliation", result)

    def test_pre_execution_rejection_unknown_command_metadata_mixed_batch_and_raw_claims_block(self):
        base = {"command_type": "source_read", "command_line": '{"path":"src/file.py","end_line": ninety}'}
        for change in ({"command_type": "zsh"}, {"command": "zsh"}, {"step": True}, {"id": False},
                       {"timeout_ms": True}, {"stall_timeout_ms": 0}, {"unknown_semantic_field": 1}):
            with self.subTest(command=change):
                result = self.inspect_resolution(self.source_read_rejection_fixture(command=dict(base, **change)))
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
        for case in ("prefix_only", "extra_error", "extra_authority", "mixed_batch", "exit_code"):
            fixture = self.source_read_rejection_fixture()
            item = fixture["rejected"]
            if case == "prefix_only":
                item["aggregated_output"] = "source_read JSON syntax error; process not started"
            elif case in ("extra_error", "extra_authority"):
                item["aggregated_output"] = {"pre_execution_rejection": fixture["rejection_proof"],
                                             "error" if case == "extra_error" else "authority_effect": "permission_denied"}
            elif case == "mixed_batch":
                item.update(command_type="command_run", command=json.dumps({"commands": [base, dict(base)]}))
            else:
                item["exit_code"] = 1
            self.republish_terminal_evidence(fixture["terminal"], fixture["run"], fixture["events"])
            with self.subTest(claim=case):
                self.assertEqual(self.inspect_resolution(fixture)["first_blocker"], "COMMAND_FAILURE")

    def test_pre_execution_rejection_keeps_later_real_unknown_and_authority_failures_blocked(self):
        for case in ("source_read", "shell", "unknown"):
            fixture = self.source_read_rejection_fixture()
            item = fixture["verifier"](1, "later")
            payload = item["aggregated_output"]
            if case == "source_read":
                item.update(command_type="source_read", command=json.dumps({"path": "src/file.py", "start_line": 0}))
                payload["error"] = "SOURCE_READ_RANGE_OUT_OF_BOUNDS"
                payload["terminal_receipt"]["termination_origin"] = "in_process_source_read"
            elif case == "shell":
                item.update(command_type="zsh", command="false")
            else:
                item.update(command_type="zsh", command="unknown", exit_code=None)
                item["aggregated_output"] = {"outcome": "unknown"}
            if case != "unknown":
                path = fixture["run"] / "execution-state/command_receipts/later.json"
                store(path, payload["terminal_receipt"])
                payload["terminal_receipt_path"] = str(path)
            fixture["events"].insert(len(fixture["events"]) - 3, {"type": "item.completed", "item": item})
            self.republish_terminal_evidence(fixture["terminal"], fixture["run"], fixture["events"])
            with self.subTest(later=case):
                result = self.inspect_resolution(fixture)
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertEqual(result["counts"]["failed"], 2)
                self.assertEqual(result["source_read_rejection_reconciliation"]["unresolved_count"], 1)
        fixture = self.source_read_rejection_fixture()
        failed = fixture["verifier"](1, "extra")
        payload = failed["aggregated_output"]
        path = fixture["run"] / "execution-state/command_receipts/extra.json"
        store(path, payload["terminal_receipt"])
        payload["terminal_receipt_path"] = str(path)
        fixture["events"].insert(3, {"type": "item.completed", "item": failed})
        self.republish_terminal_evidence(fixture["terminal"], fixture["run"], fixture["events"])
        result = self.inspect_resolution(fixture)
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(result["counts"]["failed"], 2)
        self.assertEqual(result["verifier_failure_resolution"]["resolved_count"], 1)
        self.assertEqual(result["source_read_rejection_reconciliation"]["not_attempted_count"], 1)

    def test_pre_execution_rejection_retains_index_file_cleanup_terminal_and_normal_result_gates(self):
        with patch.object(full_core, "MAX_COMMAND_EVIDENCE", 1):
            fixture = self.source_read_rejection_fixture()
            result = self.inspect_resolution(fixture)
        self.assertEqual(result["first_blocker"], "EVIDENCE_INDEX_PARTIAL")
        for case in ("file", "cleanup", "terminal", "worker_blocked"):
            fixture = self.source_read_rejection_fixture()
            terminal = fixture["terminal"]
            if case == "file":
                path = fixture["run"] / "file-change-evidence.json"
                proof = json.loads(path.read_text())
                proof["records"][0]["event_sha256"] = "b" * 64
                store(path, proof)
                terminal["file_change_evidence_artifact"] = ref(path)
            elif case == "cleanup":
                terminal["cleanup_pass"] = False
            elif case == "terminal":
                terminal.update(status="BLOCKED", first_typed_blocker="ORIGINAL_AUTHORITY_FAILURE")
            else:
                marker = terminal["terminal_evidence"]
                marker["terminal_status"] = "blocked"
                terminal["result_text"] = caller._canonical_bytes({key: marker[key] for key in
                    ("terminal_status", "delivery_mode", "parent_acceptance_required",
                     "final_summary_turn_executed")}).decode()
            self.republish_terminal_evidence(terminal, fixture["run"], fixture["events"])
            with self.subTest(gate=case):
                result = self.inspect_resolution(fixture)
                self.assertEqual(result["status"], "INCOMPLETE_EVIDENCE")
                self.assertIsNotNone(result["first_blocker"])
                self.assertEqual(result["command_success"], "known_failure")
        normal = self.inspect()
        self.assertNotIn("source_read_rejection_reconciliation", normal)
        self.assertTrue(all("pre_execution_rejection" not in row for row in normal["commands"]))

    def test_source_bound_fail_patch_pass_resolves_without_erasing_history(self):
        fixture = self.resolution_fixture()
        result = self.inspect_resolution(fixture)
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        self.assertIsNone(result["first_blocker"])
        self.assertEqual(result["command_success"], "known_failure")
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertEqual(result["verifier_failure_resolution"], {
            "schema_version": "nokiy_focused_verifier_resolution_v1", "resolved_count": 1,
            "unresolved_count": 0, "effective_command_success": "known_success"})
        failed = result["commands"][0]
        self.assertEqual((failed["exit_code"], failed["execution_proof"]), (1, "known_failure"))
        self.assertEqual(failed["failure_resolution"], {"status": "resolved", "verifiers": [{
            "passing_event_index": 2, "verifier_index": 0,
            "verifier_sha256": caller._canonical_sha256(fixture["contract"]["verifier_commands"][0]),
            "failed_call_id": "parent-failed", "passing_call_id": "parent-passed"}]})
        self.assertEqual(result["mission_acceptance"], "parent_owned")

    def test_legacy_pass_and_advisory_context_cannot_resolve_the_original_failure(self):
        def legacy(state):
            payload = state["passed"]["aggregated_output"]
            payload["source_postimages"] = payload.pop("verification_evidence")
        result = self.inspect_resolution(self.resolution_fixture(legacy))
        self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
        self.assertEqual(result["verifier_failure_resolution"]["resolved_count"], 0)

    def test_verifier_evidence_rejects_partial_malformed_foreign_and_type_confused_values(self):
        changes = [("schema_version", "foreign"), ("authorization_semantic_sha256", "b" * 64),
                   ("verifier_index", True), ("verifier_index", -1), ("verifier_index", 1),
                   ("verifier_index", 0.0), ("verifier_sha256", "b" * 64),
                   ("call_id", "parent-failed"), ("source_postimages", {}), ("source_postimages", [])]
        mutations = [lambda value, key=key, replacement=replacement: value.update({key: replacement})
                     for key, replacement in changes]
        mutations += [lambda v: v.update(extra=True), lambda v: v.pop("call_id"),
                      lambda v: v["source_postimages"].pop("src/unchanged.txt"),
                      lambda v: v["source_postimages"].update({"foreign.py": dict(v["source_postimages"]["src/file.py"])})]
        for key, replacement in (("sha256", "A" * 64), ("sha256", "b" * 64), ("bytes", -1),
                                 ("bytes", True), ("bytes", 4.0), ("bytes", 99),
                                 ("mode", True), ("mode", -1), ("mode", 0o600), ("mode", 0o10000)):
            mutations.append(lambda v, key=key, replacement=replacement:
                             v["source_postimages"]["src/file.py"].update({key: replacement}))
        mutations += [lambda v: v["source_postimages"]["src/file.py"].pop("mode"),
                      lambda v: v["source_postimages"]["src/file.py"].update(extra=True)]
        for number, mutation in enumerate(mutations):
            with self.subTest(number=number):
                fixture = self.resolution_fixture(lambda s: mutation(s["passed"]["aggregated_output"]["verification_evidence"]))
                result = self.inspect_resolution(fixture)
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertEqual(result["verifier_failure_resolution"]["resolved_count"], 0)

    def test_only_authenticated_settled_workload_failures_and_passes_are_eligible(self):
        mutations = [lambda s: s["failed"].update(command_type="zsh"),
                     lambda s: s["failed"]["aggregated_output"].update(executor="foreign"),
                     lambda s: s["failed"]["aggregated_output"]["terminal_receipt"].update(termination_origin="workload"),
                     lambda s: s["failed"]["aggregated_output"].pop("terminal_receipt"),
                     lambda s: s["failed"]["aggregated_output"].update(outcome="unknown"),
                     lambda s: s["failed"]["aggregated_output"].update(isError=True),
                     lambda s: s["failed"]["aggregated_output"]["terminal_receipt"].update(failure_class="transport"),
                     lambda s: s["failed"]["aggregated_output"]["terminal_receipt"].pop("failure_class"),
                     lambda s: s["passed"]["aggregated_output"].update(executor="foreign"),
                     lambda s: s["passed"]["aggregated_output"]["terminal_receipt"].update(failure_class="transport"),
                     lambda s: s["passed"]["aggregated_output"].update(outcome="unknown"),
                     lambda s: s["passed"]["aggregated_output"].update(process_reaped=False),
                     lambda s: s["passed"]["aggregated_output"].update(process_group_empty=False)]
        for flag in ("cancelled", "timed_out"):
            for name in ("failed", "passed"):
                mutations.append(lambda s, flag=flag, name=name: s[name]["aggregated_output"].update({flag: True}))
        for name in ("failed", "passed"):
            for flag in ("process_reaped", "process_group_empty", "termination_proven"):
                mutations.append(lambda s, name=name, flag=flag:
                                 s[name]["aggregated_output"]["terminal_receipt"].update({flag: False}))
        def signalled(s):
            s["failed"]["exit_code"] = -9
            s["failed"]["aggregated_output"].update(exit_code=-9)
            s["failed"]["aggregated_output"]["terminal_receipt"].update(exit_code=-9)
        mutations.append(signalled)
        for number, mutation in enumerate(mutations):
            with self.subTest(number=number):
                result = self.inspect_resolution(self.resolution_fixture(mutation))
                self.assertEqual(result["status"], "INCOMPLETE_EVIDENCE")
                self.assertIsNotNone(result["first_blocker"])
                self.assertFalse(any("failure_resolution" in row for row in result["commands"]))

    def test_same_index_entire_grant_call_identity_and_strict_order_are_required(self):
        def other_index(s):
            s["contract"]["verifier_commands"].append(deepcopy(s["contract"]["verifier_commands"][0]))
            s["passed"]["command"] = json.dumps({"verifier_index": 1})
            s["passed"]["aggregated_output"]["verification_evidence"]["verifier_index"] = 1
        def late_mutation(s):
            event = deepcopy(s["events"][1])
            event["item"]["id"] = "patch-late"
            s["events"].append(event)
        def reused_call(s):
            payload = s["passed"]["aggregated_output"]
            payload["terminal_receipt"]["call_id"] = "parent-failed"
            payload["verification_evidence"]["call_id"] = "parent-failed"
        def ambiguous_patch_pass(s):
            patch_item = deepcopy(self.item)
            patch_item.update(command_type="apply_patch", command="patch")
            s["events"][2]["item"] = self.batch_item(patch_item, s["passed"])
        mutations = [other_index, late_mutation, reused_call, ambiguous_patch_pass,
                     lambda s: s["events"].reverse(),
                     lambda s: s["contract"]["verifier_commands"][0]["python_import_roots"].append("/foreign"),
                     lambda s: s["contract"]["verifier_commands"][0]["pinned_files"][0].update(sha256="c" * 64),
                     lambda s: s["failed"].update(command=json.dumps({"verifier_index": True})),
                     lambda s: s["passed"].update(command=json.dumps({"verifier_index": -1}))]
        for number, mutation in enumerate(mutations):
            with self.subTest(number=number):
                result = self.inspect_resolution(self.resolution_fixture(mutation))
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertEqual(result["verifier_failure_resolution"]["resolved_count"], 0)

    def test_every_unchanged_target_needs_a_bound_initial_snapshot(self):
        mutations = [lambda s: s["contract"]["dcf_generation"].pop("source_snapshot"),
                     lambda s: s["contract"]["dcf_generation"]["source_snapshot"].pop("src/unchanged.txt"),
                     lambda s: s["contract"]["dcf_generation"]["source_snapshot"]["src/unchanged.txt"].update(mode=True),
                     lambda s: s["contract"]["dcf_generation"]["source_snapshot"]["src/unchanged.txt"].update(sha256="b" * 64),
                     lambda s: s["contract"].update(declared_targets=["src/file.py"])]
        for number, mutation in enumerate(mutations):
            with self.subTest(number=number):
                self.assertEqual(self.inspect_resolution(self.resolution_fixture(mutation))["first_blocker"], "COMMAND_FAILURE")
        fixture = self.resolution_fixture(lambda s: s["contract"]["dcf_generation"]["source_snapshot"]["src/unchanged.txt"].pop("bytes"))
        self.assertEqual(self.inspect_resolution(fixture)["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(self.inspect_resolution(self.resolution_fixture(targets=(), changed=False))["status"], "EVIDENCE_VERIFIED")

    def test_compact_parent_receipts_supply_cleanup_without_redundant_payload_flags(self):
        def compact(s):
            for name in ("failed", "passed"):
                item = s[name]
                item.pop("command_type")
                for key in ("success", "outcome", "process_reaped", "process_group_empty"):
                    item["aggregated_output"].pop(key)
        self.assertEqual(self.inspect_resolution(self.resolution_fixture(compact))["status"], "EVIDENCE_VERIFIED")

    def test_later_unknown_non_verifier_and_new_verifier_failures_still_block(self):
        for kind in ("verifier", "shell", "unknown"):
            def append(s):
                item = s["verifier"](1, "later")
                if kind == "shell":
                    item.update(command_type="zsh", command="false")
                    item["aggregated_output"]["terminal_receipt"]["termination_origin"] = "workload"
                elif kind == "unknown":
                    item["aggregated_output"].update(outcome="unknown")
                s["events"].append({"type": "item.completed", "item": item})
            with self.subTest(kind=kind):
                result = self.inspect_resolution(self.resolution_fixture(append))
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertEqual(result["counts"]["failed"], 2)
                self.assertEqual(result["verifier_failure_resolution"]["resolved_count"], 1)
                self.assertEqual(result["verifier_failure_resolution"]["unresolved_count"], 1)

    def test_authenticated_verifier_only_batches_resolve_but_mixed_failures_cannot(self):
        def wrap(s, mixed=False):
            for name in ("failed", "passed"):
                child = s[name]
                wrapper = self.batch_item(child)
                wrapper["aggregated_output"]["terminal_receipt"] = deepcopy(self.receipt)
                wrapper["aggregated_output"]["terminal_receipt"].update(termination_origin="workload", call_id="batch-" + name)
                if mixed and name == "failed":
                    shell = deepcopy(child)
                    shell.update(command_type="zsh", command="false")
                    shell["aggregated_output"]["terminal_receipt"].update(termination_origin="workload", call_id="shell-failed")
                    extra = self.batch_item(shell)
                    args = json.loads(wrapper["command"])
                    args["commands"].extend(json.loads(extra["command"])["commands"])
                    wrapper["command"] = json.dumps(args)
                    wrapper["aggregated_output"]["results"].extend(extra["aggregated_output"]["results"])
                s["events"][0 if name == "failed" else 2]["item"] = wrapper
        result = self.inspect_resolution(self.resolution_fixture(wrap))
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        result = self.inspect_resolution(self.resolution_fixture(lambda s: wrap(s, True)))
        self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
        self.assertEqual(result["verifier_failure_resolution"]["resolved_count"], 0)

    def test_partial_command_or_invalid_file_evidence_cannot_resolve(self):
        with patch.object(full_core, "MAX_COMMAND_EVIDENCE", 1):
            result = self.inspect_resolution(self.resolution_fixture())
        self.assertEqual(result["first_blocker"], "EVIDENCE_INDEX_PARTIAL")
        fixture = self.resolution_fixture()
        path = fixture["run"] / "file-change-evidence.json"
        proof = json.loads(path.read_text())
        proof["records"][0]["event_sha256"] = "b" * 64
        store(path, proof)
        fixture["terminal"]["file_change_evidence_artifact"] = ref(path)
        store(fixture["run"] / "terminal.json", fixture["terminal"])
        result = self.inspect_resolution(fixture)
        self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
        self.assertEqual(result["verifier_failure_resolution"]["resolved_count"], 0)

    def test_original_contract_and_final_postimage_identity_must_remain_bound(self):
        for artifact in ("jspace-original.json", "jspace.json", "file-change-evidence.json"):
            with self.subTest(artifact=artifact):
                fixture = self.resolution_fixture()
                path = fixture["run"] / artifact
                value = json.loads(path.read_text())
                if artifact == "file-change-evidence.json":
                    value["postimages"][0]["sha256"] = "b" * 64
                else:
                    value["authorization_semantic_sha256"] = "b" * 64
                store(path, value)
                key = "file_change_evidence_artifact" if artifact == "file-change-evidence.json" else "original_jspace_artifact"
                if artifact != "jspace.json":
                    fixture["terminal"][key] = ref(path)
                store(fixture["run"] / "terminal.json", fixture["terminal"])
                result = self.inspect_resolution(fixture)
                self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
                self.assertEqual(result["verifier_failure_resolution"]["resolved_count"], 0)
        fixture = self.resolution_fixture(targets=(), changed=False)
        path = fixture["run"] / "jspace-original.json"
        value = json.loads(path.read_text())
        value["authorization_semantic_sha256"] = "b" * 64
        store(path, value)
        result = self.inspect_resolution(fixture)
        self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
        self.assertEqual(result["verifier_failure_resolution"]["resolved_count"], 0)

    def test_resolution_projection_summary_and_snapshot_are_offline_and_keep_metadata(self):
        fixture = self.resolution_fixture()
        terminal, run = fixture["terminal"], fixture["run"]
        before = {p: p.read_bytes() for p in (run / "terminal.json", run / "core.jsonl", run / "command-evidence.json")}
        (fixture["workspace"] / "src/file.py").write_bytes(b"later unrecorded source\n")
        iterations = []
        iterate = full_core._Trajectory.__iter__
        def counted(trajectory):
            iterations.append(trajectory)
            return iterate(trajectory)
        with patch.object(full_core._Trajectory, "__iter__", counted), \
                patch.object(file_change_evidence, "_postimage", side_effect=AssertionError("no live source")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no bulk reads")), \
                patch.object(Path, "read_text", side_effect=AssertionError("no bulk reads")):
            projected = self.project_capability(terminal)
        self.assertEqual(len(iterations), 3)  # Existing command, file-proof and receipt passes only.
        compact = projected["result_inspection"]
        self.assertEqual(compact["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(compact["commands"][0]["execution_proof"], "known_failure")
        self.assertIn("failure_resolution", compact["commands"][0])
        with patch.object(inspector, "inspect", side_effect=AssertionError("no reinspection")):
            summary = inspector.summarize_terminal(projected, self.root, terminal["request_id"])
            reference = inspector.publish_inspection_summary(projected, self.root, terminal["request_id"], expected_thread_id=THREAD)
            retained = inspector.load_inspection_summary(reference, expected_request_id=terminal["request_id"],
                expected_thread_id=THREAD, terminal_reference={"path": str(run / "terminal.json"),
                "sha256": hashlib.sha256(before[run / "terminal.json"]).hexdigest()})
        for value in (summary, retained):
            self.assertEqual(value["result_inspection"], compact)
            self.assertEqual(value["mission_acceptance"] if "mission_acceptance" in value else
                             value["result_inspection"]["mission_acceptance"], "parent_owned")
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_priority_25_reads_then_verifier_inspects_once_without_mutation(self):
        read, verifier = self.source_read_item(), self.verifier_item()
        self.events = [{"type": "item.completed", "item": deepcopy(read)} for _ in range(25)]
        self.events.append({"type": "item.completed", "item": verifier})
        self.publish()
        before = {path: path.read_bytes() for path in (self.run / "terminal.json", self.receipt_path)}
        history = self.inspect()
        self.assertEqual([row["event_index"] for row in history["commands"]], list(range(16)))
        self.assertEqual(history["pagination"]["next_offset"], 16)
        self.assertNotIn("stream", history["pagination"])
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
             patch.object(inspector, "inspect", wraps=inspector.inspect) as inspect:
            projected = inspector.project_terminal(self.terminal, self.root, RID)
        inspect.assert_called_once_with(self.root, RID, THREAD, command_stream="priority")
        evidence = projected["result_inspection"]
        self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(evidence["counts"]["commands"], 26)
        self.assertEqual(evidence["pagination"], {"offset": 0, "limit": 16, "next_offset": None,
                         "stream": "priority", "omitted_source_read_commands": 25, "total_commands": 1})
        self.assertEqual(evidence["commands"][0]["event_index"], 25)
        self.assertEqual(evidence["commands"][0]["diagnostic_excerpt"],
                         verifier["aggregated_output"]["stdout"])
        self.assertNotIn("SOURCE_BODY_NOT_FOR_PROJECTION", json.dumps(projected))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertNotIn("result_inspection", self.terminal)

    def test_priority_read_with_postimages_is_not_read_only(self):
        item = self.source_read_item()
        item["aggregated_output"]["source_postimages"] = {"files": [{"path": "src/example.py"}]}
        self.events = [{"type": "item.completed", "item": item}]
        self.publish()
        result = self.inspect(command_stream="priority")
        self.assertEqual(result["pagination"]["omitted_source_read_commands"], 0)
        self.assertEqual(len(result["commands"]), 1)

    def test_priority_current_runtime_types_come_from_matched_durable_receipts(self):
        read, verifier = self.source_read_item(), self.verifier_item()
        for item, origin in ((read, "in_process_source_read"), (verifier, "parent_verifier")):
            item.pop("command_type")
            output = item["aggregated_output"]
            receipt = {**self.receipt, "termination_origin": origin}
            path = self.receipt_path.with_name(origin + ".json")
            store(path, receipt)
            output.update(terminal_receipt=receipt, terminal_receipt_path=str(path))
        verifier["aggregated_output"]["executor"] = "parent_focused_verifier"
        self.events = [{"type": "item.completed", "item": deepcopy(read)} for _ in range(25)]
        self.events.append({"type": "item.completed", "item": verifier})
        self.publish()
        result = self.inspect(command_stream="priority")
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(result["pagination"]["omitted_source_read_commands"], 25)
        self.assertEqual(result["commands"][0]["event_index"], 25)
        self.assertEqual(result["commands"][0]["verifier_output_excerpt"],
                         verifier["aggregated_output"]["stdout"])
        for invalid in ("receipt_missing", "wrong_origin", "explicit_shell"):
            item = deepcopy(read)
            output = item["aggregated_output"]
            if invalid == "receipt_missing":
                output.pop("terminal_receipt")
                output.pop("terminal_receipt_path")
            elif invalid == "wrong_origin":
                receipt = {**self.receipt, "termination_origin": "shell"}
                store(self.receipt_path, receipt)
                output.update(terminal_receipt=receipt, terminal_receipt_path=str(self.receipt_path))
            else:
                item["command_type"] = "shell"
            self.events = [{"type": "item.completed", "item": item}]
            self.publish()
            with self.subTest(invalid=invalid):
                result = self.inspect(command_stream="priority")
                self.assertEqual(result["pagination"]["omitted_source_read_commands"], 0)
                self.assertEqual(len(result["commands"]), 1)

    def test_priority_never_omits_failed_unknown_spoofed_or_mixed_reads(self):
        read = self.source_read_item()
        failed = deepcopy(read)
        failed.update(status="failed", exit_code=1, success=False)
        failed_receipt = {**self.receipt, "exit_code": 1, "terminal_state": "failed"}
        failed_path = self.receipt_path.with_name("failed.json")
        store(failed_path, failed_receipt)
        failed["aggregated_output"].update(exit_code=1, terminal_receipt=failed_receipt,
                                           terminal_receipt_path=str(failed_path))
        unknown = deepcopy(read)
        unknown["aggregated_output"].pop("terminal_receipt")
        unknown["aggregated_output"].pop("terminal_receipt_path")
        unknown_status = deepcopy(read)
        unknown_status["aggregated_output"]["status"] = "unknown"
        unknown_outcome = deepcopy(read)
        unknown_outcome["aggregated_output"]["outcome"] = "unknown"
        failed_output = deepcopy(read)
        failed_output["aggregated_output"]["status"] = "failed"
        bool_success = deepcopy(read)
        bool_success["aggregated_output"]["success"] = 0
        untyped = deepcopy(read)
        untyped.pop("command_type")
        spoof = deepcopy(read)
        spoof.update(command_type="shell", command="echo source_read")
        spoof_input = deepcopy(read)
        spoof_input["command"] = json.dumps({"commands": [{"command_type": "apply_patch"}]})
        spoof_path = deepcopy(read)
        spoof_path["command"] = json.dumps({"path": "another.py"})
        spoof_sha = deepcopy(read)
        spoof_sha["command"] = json.dumps({"path": "src/example.py", "expected_sha256": "c" * 64})
        malformed = deepcopy(read)
        malformed["aggregated_output"]["stdout"] = "not a proven physical source line"
        effect = deepcopy(self.item)
        effect.update(command_type="apply_patch", command="apply_patch")
        effect["aggregated_output"].update(stdout="POSTIMAGE_NOT_FOR_PROJECTION", changes=[])
        spoof_batch = self.batch_item(read, effect)
        spoof_batch["aggregated_output"]["results"][1]["command_type"] = "source_read"
        cases = [("failed", failed, 0, "COMMAND_FAILURE"),
                 ("unknown", unknown, 0, "EXECUTION_PROOF_UNAVAILABLE"),
                 ("unknown_status", unknown_status, 0, "EXECUTION_PROOF_UNAVAILABLE"),
                 ("unknown_outcome", unknown_outcome, 0, "EXECUTION_PROOF_UNAVAILABLE"),
                 ("failed_output", failed_output, 0, "COMMAND_FAILURE"),
                 ("bool_success", bool_success, 0, "EXECUTION_PROOF_UNAVAILABLE"),
                 ("untyped", untyped, 0, None), ("spoof", spoof, 0, None),
                 ("spoof_input", spoof_input, 0, None), ("spoof_path", spoof_path, 0, None),
                 ("spoof_sha", spoof_sha, 0, None),
                 ("malformed", malformed, 0, None),
                 ("mixed", self.batch_item(read, effect), 0, None),
                 ("spoof_batch", spoof_batch, 0, None),
                 ("failed_child", self.batch_item(read, failed), 0, "COMMAND_FAILURE"),
                 ("unknown_child", self.batch_item(read, unknown), 0, "EXECUTION_PROOF_UNAVAILABLE"),
                 ("read_only_batch", self.batch_item(read, read), 1, None)]
        for name, item, omitted, blocker in cases:
            with self.subTest(name=name):
                self.events = [{"type": "item.completed", "item": item}]
                self.publish()
                with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}):
                    projected = inspector.project_terminal(self.terminal, self.root, RID)
                evidence = projected["result_inspection"]
                self.assertEqual(evidence["pagination"]["omitted_source_read_commands"], omitted)
                self.assertEqual(len(evidence["commands"]), 1 - omitted)
                self.assertEqual(evidence["first_blocker"], blocker)
                self.assertNotIn("diagnostic_excerpt", evidence["commands"][0] if not omitted else {})
                self.assertNotIn("POSTIMAGE_NOT_FOR_PROJECTION", json.dumps(projected))

    def test_priority_cursors_have_matching_cli_and_legacy_offsets_remain_history(self):
        self.events = ([{"type": "item.completed", "item": self.source_read_item()} for _ in range(25)]
                       + [{"type": "item.completed", "item": self.verifier_item()} for _ in range(20)])
        self.publish()
        first = self.inspect(command_stream="priority")
        self.assertEqual([row["event_index"] for row in first["commands"]], list(range(25, 41)))
        self.assertEqual(first["pagination"]["next_offset"], 16)
        base = ["result-inspection", "--artifact-root", str(self.root), "--request-id", RID,
                "--expected-thread-id", THREAD, "--offset", "16"]
        for options, expected, stream in ((["--command-stream", "priority"], list(range(41, 45)), "priority"),
                                           ([], list(range(16, 32)), None)):
            output = io.StringIO()
            with patch("sys.argv", base + options), redirect_stdout(output):
                code = inspector.main()
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual([row["event_index"] for row in result["commands"]], expected)
            self.assertEqual(result["pagination"].get("stream"), stream)
        self.assertEqual(self.inspect(command_stream="priority", offset=21)["first_blocker"],
                         "OFFSET_OUT_OF_RANGE")
        self.assertEqual(self.inspect(command_stream="unknown")["first_blocker"], "COMMAND_STREAM_INVALID")

    def test_priority_byte_truncation_cursors_retrieve_the_same_filtered_stream(self):
        verifier = self.verifier_item()
        verifier["command"] = "x" * inspector.MAX_COMMAND_BYTES
        self.events = ([{"type": "item.completed", "item": self.source_read_item()} for _ in range(25)]
                       + [{"type": "item.completed", "item": deepcopy(verifier)} for _ in range(20)])
        self.publish()
        offset, seen = 0, []
        with patch.object(inspector, "MAX_PAGE_BYTES", 6000):
            while True:
                page = self.inspect(command_stream="priority", offset=offset)
                self.assertEqual(page["status"], "EVIDENCE_VERIFIED")
                self.assertLessEqual(len(caller._canonical_bytes(page)), 6000)
                self.assertEqual(page["pagination"]["omitted_source_read_commands"], 25)
                seen.extend(row["event_index"] for row in page["commands"])
                next_offset = page["pagination"]["next_offset"]
                if next_offset is None:
                    break
                self.assertGreater(next_offset, offset)
                offset = next_offset
        self.assertEqual(seen, list(range(25, 45)))
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
             patch.object(inspector, "MAX_PROJECTED_BYTES", 6000):
            projected = inspector.project_terminal(self.terminal, self.root, RID)
        evidence = projected["result_inspection"]
        self.assertEqual(evidence["first_blocker"], "PROJECTION_PARTIAL")
        self.assertEqual(evidence["projection_blocker"], "PROJECTION_PARTIAL")
        self.assertEqual(evidence["pagination"]["stream"], "priority")
        self.assertLessEqual(len(caller._canonical_bytes(projected)), 6000)
        cursor = evidence["pagination"]["next_offset"]
        self.assertEqual(cursor, len(evidence["commands"]))
        resumed = self.inspect(command_stream="priority", offset=cursor)
        self.assertEqual(resumed["commands"][0]["event_index"], 25 + cursor)
        self.assertEqual(self.inspect(offset=cursor)["commands"][0]["event_index"], cursor)

    def test_priority_global_late_failure_survives_selection_and_projection_truncation(self):
        failed = deepcopy(self.item)
        failed.update(status="failed", exit_code=9)
        receipt = {**self.receipt, "exit_code": 9, "terminal_state": "failed"}
        path = self.receipt_path.with_name("late-failure.json")
        store(path, receipt)
        failed["aggregated_output"].update(exit_code=9, terminal_receipt=receipt, terminal_receipt_path=str(path))
        self.events = ([{"type": "item.completed", "item": self.source_read_item()} for _ in range(25)]
                       + [{"type": "item.completed", "item": self.verifier_item()} for _ in range(20)]
                       + [{"type": "item.completed", "item": failed}])
        self.publish()
        for stream in ("history", "priority"):
            result = self.inspect(command_stream=stream)
            self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
            self.assertEqual(result["command_success"], "known_failure")
            self.assertNotIn(45, [row["event_index"] for row in result["commands"]])
        selected = hashlib.sha256(self.verifier_item()["command"].encode()).hexdigest()
        self.assertEqual(self.inspect(command_stream="priority", command_sha256=(selected,))["first_blocker"],
                         "COMMAND_FAILURE")
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
             patch.object(inspector, "MAX_PROJECTED_BYTES", 3000):
            projected = inspector.project_terminal(self.terminal, self.root, RID)
        evidence = projected["result_inspection"]
        self.assertEqual(evidence["first_blocker"], "COMMAND_FAILURE")
        self.assertEqual(evidence["command_success"], "known_failure")
        self.assertTrue(evidence["projection_partial"])
        self.assertIn(evidence["projection_blocker"], ("PROJECTION_PARTIAL", "PROJECTION_TOO_LARGE"))
        self.assertEqual(evidence["pagination"]["stream"], "priority")
        self.assertLessEqual(len(caller._canonical_bytes(projected)), 3000)
        late = self.inspect(command_stream="priority", offset=20)
        self.assertEqual(late["commands"][0]["event_index"], 45)
        self.assertEqual(late["commands"][0]["exit_code"], 9)

    def test_priority_partial_index_does_not_count_unindexed_reads_or_claim_success(self):
        self.events = ([{"type": "item.completed", "item": self.source_read_item()} for _ in range(4)]
                       + [{"type": "item.completed", "item": self.verifier_item()}])
        with patch.object(full_core, "MAX_COMMAND_EVIDENCE", 2):
            self.publish()
            result = self.inspect(command_stream="priority")
        self.assertEqual(result["first_blocker"], "EVIDENCE_INDEX_PARTIAL")
        self.assertEqual(result["command_success"], "unproven")
        self.assertFalse(result["counts"]["index_complete"])
        self.assertEqual(result["pagination"]["omitted_source_read_commands"], 1)
        self.assertEqual([row["event_index"] for row in result["commands"]], [4])

    def test_priority_checks_late_and_nested_receipts_before_display(self):
        bad = self.source_read_item()
        path = self.receipt_path.with_name("mismatched.json")
        store(path, {**self.receipt, "extra": "mismatch"})
        bad["aggregated_output"]["terminal_receipt_path"] = str(path)
        for item in (bad, self.batch_item(self.source_read_item(), bad)):
            self.events = ([{"type": "item.completed", "item": self.source_read_item()} for _ in range(25)]
                           + [{"type": "item.completed", "item": self.verifier_item()},
                              {"type": "item.completed", "item": item}])
            self.publish()
            result = self.inspect(command_stream="priority")
            self.assertEqual(result["first_blocker"], "RECEIPT_MISMATCH")
            self.assertEqual(result["commands"], [])
            self.assertNotIn("omitted_source_read_commands", result["pagination"])

    def test_priority_verifier_diagnostic_is_byte_bounded_without_source_or_effect_dumps(self):
        verifier = self.verifier_item("\u5b57" * 600 + "\n10 passed\n")
        mixed = self.batch_item(self.source_read_item(), verifier)
        effect = deepcopy(self.item)
        effect.update(command_type="apply_patch", command="apply_patch")
        effect["aggregated_output"].update(stdout="POSTIMAGE_NOT_FOR_PROJECTION", changes=[])
        self.events = [{"type": "item.completed", "item": item} for item in (mixed, effect)]
        self.publish()
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}):
            projected = inspector.project_terminal(self.terminal, self.root, RID)
        rows = projected["result_inspection"]["commands"]
        self.assertEqual(len(rows), 2)
        diagnostic = rows[0]["diagnostic_excerpt"]
        self.assertLessEqual(len(diagnostic.encode()), inspector.MAX_DIAGNOSTIC_BYTES)
        self.assertTrue(diagnostic.endswith("10 passed\n"))
        self.assertNotIn("diagnostic_excerpt", rows[1])
        self.assertNotIn("SOURCE_BODY_NOT_FOR_PROJECTION", json.dumps(projected))
        self.assertNotIn("POSTIMAGE_NOT_FOR_PROJECTION", json.dumps(projected))

    def test_cli_run_projects_verified_commands_without_reexecution_or_receipt_mutation(self):
        request = SimpleNamespace(artifact_root=self.root, request_id=RID, native_thread_id=THREAD)
        self.terminal["result_text"] = "original result"
        store(self.run / "terminal.json", self.terminal)
        for count in (4, 11):
            self.events = [self.events[0].copy() for _ in range(count)] + [self.events[-1]]
            self.publish()
            self.terminal["result_text"] = "original result"
            store(self.run / "terminal.json", self.terminal)
            original = (self.run / "terminal.json").read_bytes()
            with (self.loaded_request(request),
                  patch.object(caller, "execute", return_value=self.terminal) as execute,
                  patch.object(caller, "read_terminal", wraps=caller.read_terminal) as raw):
                code, result = self.cli(["run", "--request", str(self.root / "unused.json")])
            self.assertEqual(code, 0)
            execute.assert_called_once_with(request)
            raw.assert_called_once_with(self.root, RID)
            self.assertEqual(result["result_text"], "original result")
            self.assertEqual(result["status"], "RESULT_AVAILABLE")
            evidence = result["result_inspection"]
            self.assertEqual(evidence["status"], "EVIDENCE_VERIFIED")
            self.assertEqual([row["command"] for row in evidence["commands"]], [self.command] * count)
            self.assertEqual([row["execution_proof"] for row in evidence["commands"]],
                             ["known_success"] * count)
            self.assertNotIn("usage", evidence)
            self.assertNotIn("artifacts", evidence)
            self.assertIn("terminal_sha256", evidence)
            self.assertEqual(set(evidence["commands"][0]),
                             {"event_index", "command", "exit_code", "receipt_check", "execution_proof"})
            self.assertEqual((self.run / "terminal.json").read_bytes(), original)

    def test_cli_read_expired_recovery_and_pagination(self):
        store(self.run / "execution.json", {"request": {"expires_at": 0}})
        self.events = [self.events[0].copy() for _ in range(25)] + [self.events[-1]]
        self.publish()
        with patch.object(caller, "_prepare", side_effect=AssertionError("preparation")), \
             patch.object(caller, "execute", side_effect=AssertionError("execution")):
            code, result = self.cli(["read-result", "--artifact-root", str(self.root),
                                     "--request-id", RID])
        self.assertEqual(code, 0)
        evidence = result["result_inspection"]
        self.assertEqual(len(evidence["commands"]), inspector.PAGE_SIZE)
        self.assertEqual(evidence["pagination"]["next_offset"], inspector.PAGE_SIZE)
        self.assertEqual(evidence["counts"]["commands"], 25)
        self.assertLessEqual(len(caller._canonical_bytes(result)), inspector.MAX_PROJECTED_BYTES)
        self.assertNotIn("result_inspection", caller.read_terminal(self.root, RID))

    def test_cli_identity_failure_does_not_borrow_terminal_identity(self):
        command = ["read-result", "--artifact-root", str(self.root), "--request-id", RID]
        for thread, blocker in (("", "CALLER_THREAD_ID_INVALID"),
                                ("not-a-uuid", "CALLER_THREAD_ID_INVALID"),
                                ("87654321-1234-1234-1234-123456789abc", "THREAD_ID_MISMATCH")):
            code, result = self.cli(command, thread=thread)
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "RESULT_AVAILABLE")
            self.assertEqual(result["result_inspection"]["first_blocker"], blocker)
            self.assertEqual(result["result_inspection"]["command_success"], "unproven")
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": ""}):
            self.assertEqual(inspector.project_terminal(self.terminal, self.root, RID,
                             request_thread_id=THREAD)["result_inspection"]["first_blocker"],
                             "CALLER_THREAD_ID_INVALID")
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}):
            for identity, blocker in ((None, "REQUEST_THREAD_ID_INVALID"),
                                      ("not-a-uuid", "REQUEST_THREAD_ID_INVALID"),
                                      ("87654321-1234-1234-1234-123456789abc", "CALLER_THREAD_ID_MISMATCH")):
                projected = inspector.project_terminal(self.terminal, self.root, RID,
                             request_thread_id=identity, require_request_binding=True)
                self.assertEqual(projected["result_inspection"]["first_blocker"], blocker)
                self.assertEqual(projected["result_inspection"]["command_success"], "unproven")

    def test_cli_failure_unknown_cleanup_and_unavailable_evidence(self):
        command = ["read-result", "--artifact-root", str(self.root), "--request-id", RID]
        self.item["exit_code"] = 7
        self.item["aggregated_output"]["exit_code"] = 7
        self.receipt.update(exit_code=7, terminal_state="failed")
        store(self.receipt_path, self.receipt)
        self.publish()
        code, result = self.cli(command)
        self.assertEqual(code, 0)
        self.assertEqual(result["result_inspection"]["command_success"], "known_failure")
        self.item["exit_code"] = None
        self.item["aggregated_output"] = {"exit_code": None}
        self.publish()
        code, result = self.cli(command)
        self.assertEqual(result["result_inspection"]["command_success"], "known_failure")
        self.assertEqual(result["result_inspection"]["cleanup"]["historical_pass"], True)
        self.item["exit_code"] = 0
        self.item["aggregated_output"] = {"exit_code": 0}
        self.publish()
        code, result = self.cli(command)
        self.assertEqual(result["result_inspection"]["command_success"], "unproven")
        self.assertEqual(result["result_inspection"]["first_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
        self.item["aggregated_output"] = "source stdout must not be projected"
        self.publish()
        code, result = self.cli(command)
        self.assertNotIn("source stdout must not be projected", json.dumps(result))
        self.item["aggregated_output"] = {"exit_code": 0, "terminal_receipt": self.receipt,
                                           "terminal_receipt_path": str(self.receipt_path)}
        self.receipt.update(exit_code=0, terminal_state="completed")
        store(self.receipt_path, self.receipt)
        self.supervision["scope"]["no_live_descendants"] = False
        self.publish()
        code, result = self.cli(command)
        self.assertEqual(result["result_inspection"]["first_blocker"], "CLEANUP_UNPROVEN")
        (self.run / "command-evidence.json").write_bytes(b"broken")
        code, result = self.cli(command)
        self.assertEqual(code, 0)
        self.assertEqual(result["result_inspection"]["first_blocker"], "ARTIFACT_HASH_MISMATCH")
        self.assertEqual(result["result_inspection"]["command_success"], "unproven")

    def test_cli_missing_legacy_refs_and_bounded_io_error(self):
        command = ["read-result", "--artifact-root", str(self.root), "--request-id", RID]
        self.terminal.pop("command_evidence_artifact")
        store(self.run / "terminal.json", self.terminal)
        code, result = self.cli(command)
        self.assertEqual(code, 0)
        self.assertEqual(result["result_inspection"]["first_blocker"], "ARTIFACT_REFERENCE_MISSING")
        self.assertEqual(result["result_inspection"]["command_success"], "unproven")
        with patch.object(inspector, "inspect", side_effect=ValueError("unexpected")):
            code, result = self.cli(command)
        self.assertEqual(code, 0)
        self.assertEqual(result["result_inspection"]["first_blocker"], "INSPECTION_UNAVAILABLE")
        self.assertEqual(result["status"], "RESULT_AVAILABLE")

    def test_cli_projection_budget_keeps_original_result_and_explicit_cursor(self):
        self.events = [self.events[0].copy() for _ in range(16)] + [self.events[-1]]
        self.publish()
        self.terminal["result_text"] = "original result"
        store(self.run / "terminal.json", self.terminal)
        with patch.object(inspector, "MAX_PROJECTED_BYTES", 3000):
            code, result = self.cli(["read-result", "--artifact-root", str(self.root),
                                     "--request-id", RID])
        self.assertEqual(code, 0)
        self.assertEqual(result["result_text"], "original result")
        evidence = result["result_inspection"]
        self.assertEqual(evidence["first_blocker"], "PROJECTION_PARTIAL")
        self.assertTrue(evidence["projection_partial"])
        self.assertEqual(evidence["pagination"]["next_offset"], len(evidence["commands"]))
        self.assertLessEqual(len(caller._canonical_bytes(result)), 3000)

    def test_cli_nonterminal_receipt_preserves_original_status_and_code(self):
        self.terminal["status"] = "BLOCKED"
        self.terminal["first_typed_blocker"] = "ORIGINAL_BLOCKER"
        store(self.run / "terminal.json", self.terminal)
        code, result = self.cli(["read-result", "--artifact-root", str(self.root), "--request-id", RID])
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["first_typed_blocker"], "ORIGINAL_BLOCKER")
        self.assertEqual(result["result_inspection"]["first_blocker"], "TERMINAL_NOT_AVAILABLE")

    def test_cli_other_commands_are_not_projected(self):
        request = SimpleNamespace(artifact_root=self.root, request_id=RID, native_thread_id=THREAD)
        with self.loaded_request(request), patch.object(caller, "preflight", return_value={"status": "READY"}):
            code, result = self.cli(["preflight", "--request", str(self.root / "unused.json")])
        self.assertEqual(code, 0)
        self.assertNotIn("result_inspection", result)
        with patch("codex_collaboration_harness.deployment.read_result",
                   return_value={"status": "VERIFIED"}):
            code, result = self.cli(["read-deployment-result", "--artifact-root", str(self.root),
                                     "--action-id", "unused"])
        self.assertEqual(code, 0)
        self.assertNotIn("result_inspection", result)

    def test_large_trajectory_readback_is_streamed_and_keeps_event_index(self):
        path = self.run / "core.jsonl"
        row = caller._canonical_bytes({"type": "diagnostic", "text": "x" * 65536}) + b"\n"
        count = 0
        with path.open("wb") as output:
            while output.tell() <= caller.MAX_TRAJECTORY_BYTES:
                output.write(row)
                count += 1
            for event in self.events:
                output.write(caller._canonical_bytes(event) + b"\n")
        evidence = full_core._command_evidence(full_core._Trajectory(path))
        store(self.run / "command-evidence.json", evidence)
        self.terminal["trajectory_artifact"] = ref(path)
        self.terminal["command_evidence_artifact"] = ref(self.run / "command-evidence.json")
        store(self.run / "terminal.json", self.terminal)
        tracemalloc.start()
        try:
            with patch.object(Path, "read_text", side_effect=AssertionError("bulk trajectory read")), \
                    patch.object(Path, "read_bytes", side_effect=AssertionError("bulk artifact read")):
                result = self.inspect()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED", result)
        self.assertLess(peak, 8 * 1024 * 1024)
        self.assertGreater(result["artifacts"]["trajectory"]["bytes"], caller.MAX_TRAJECTORY_BYTES)
        self.assertEqual(result["commands"][0]["event_index"], count)
        self.assertEqual(result["commands"][0]["event_sha256"], caller._canonical_sha256(self.events[0]))
        self.assertEqual(result["counts"]["events"], count + len(self.events))

    def test_last_message_artifact_is_verified_without_bulk_read(self):
        text = "head: " + "\u6e2c\u8a66" * 600000 + " :tail"
        path = self.run / "last-message.txt"
        path.write_text(text, encoding="utf-8")
        self.terminal.update(result_text=caller._bounded_preview(text, caller.MAX_RESULT_BYTES)[0],
                             result_truncated=True, result_artifact=ref(path))
        store(self.run / "terminal.json", self.terminal)
        self.assertGreater((self.run / "terminal.json").stat().st_size, 16 * 1024)
        self.assertLessEqual((self.run / "terminal.json").stat().st_size, caller.MAX_TERMINAL_BYTES)
        with patch.object(Path, "read_text", side_effect=AssertionError("bulk result read")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("bulk artifact read")):
            result = self.inspect()
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED", result)
        self.assertEqual(result["artifacts"]["result"]["bytes"], len(text.encode()))
        original = deepcopy(self.terminal["result_artifact"])
        for reference, code in ((None, "ARTIFACT_REFERENCE_MISSING"),
                                (dict(original, path=str(self.root / "foreign.txt")), "ARTIFACT_REFERENCE_INVALID"),
                                (dict(original, bytes=original["bytes"] + 1), "ARTIFACT_HASH_MISMATCH"),
                                (dict(original, sha256="0" * 64), "ARTIFACT_HASH_MISMATCH")):
            with self.subTest(code=code):
                self.terminal["result_artifact"] = reference
                store(self.run / "terminal.json", self.terminal)
                self.assertEqual(self.inspect()["first_blocker"], code)
        self.terminal["result_artifact"] = original
        store(self.run / "terminal.json", self.terminal)
        target = self.root / "foreign-result.txt"
        target.write_text(text, encoding="utf-8")
        path.unlink()
        path.symlink_to(target)
        self.assertEqual(self.inspect()["first_blocker"], "UNSAFE_FILE")
        path.unlink()
        self.assertEqual(self.inspect()["first_blocker"], "FILE_UNAVAILABLE")

    def test_streamed_malformed_and_duplicate_terminal_readback_fails_closed(self):
        path = self.run / "core.jsonl"
        for raw, code in ((b'{"type":\n', "TRAJECTORY_INVALID_JSON"),
                          (b'{"type":"x","type":"y"}\n', "TRAJECTORY_INVALID_JSON"),
                          (b'[]\n', "TRAJECTORY_INVALID_JSON"),
                          (caller._canonical_bytes(self.events[-1]) + b"\n" + caller._canonical_bytes(self.events[-1]),
                           "NOKIY_FULL_CORE_TERMINAL_INVALID")):
            with self.subTest(code=code):
                path.write_bytes(raw)
                self.terminal["trajectory_artifact"] = ref(path)
                store(self.run / "terminal.json", self.terminal)
                result = self.inspect()
                self.assertEqual(result["first_blocker"], code)
                self.assertEqual(result["artifact_integrity"], "unproven")

    def test_trajectory_checksum_size_symlink_and_in_read_drift_rejected(self):
        path = self.run / "core.jsonl"
        original = deepcopy(self.terminal["trajectory_artifact"])
        for bad in (dict(original, sha256="0" * 64), dict(original, bytes=original["bytes"] + 1)):
            self.terminal["trajectory_artifact"] = bad
            store(self.run / "terminal.json", self.terminal)
            self.assertEqual(self.inspect()["first_blocker"], "ARTIFACT_HASH_MISMATCH")
        self.terminal["trajectory_artifact"] = original
        store(self.run / "terminal.json", self.terminal)
        observe = full_core._TurnSummary.observe
        replaced = False
        def drift(turn, event):
            nonlocal replaced
            observe(turn, event)
            if not replaced:
                replaced = True
                replacement = self.run / "replacement.jsonl"
                replacement.write_bytes(b"".join(caller._canonical_bytes(item) + b"\n" for item in self.events))
                replacement.replace(path)
        with patch.object(full_core._TurnSummary, "observe", drift):
            self.assertEqual(self.inspect()["first_blocker"], "ARTIFACT_CHANGED")
        path.unlink()
        path.symlink_to(self.root / "foreign.jsonl")
        self.assertEqual(self.inspect()["first_blocker"], "UNSAFE_FILE")

    def test_retained_request_identity_cannot_downgrade_terminal_validation(self):
        store(self.run / "request-identity.json", {"schema_version": caller.REQUEST_SCHEMA_VERSION,
              "execution_profile": "direct", "request_id": RID, "request_sha256": RID.removeprefix("tura_embedded_")})
        result = self.inspect()
        self.assertEqual(result["first_blocker"], "TRAJECTORY_REQUEST_IDENTITY_MISMATCH")
        self.assertEqual(result["artifact_integrity"], "unproven")

    def test_valid_receipt_and_expired_inputs_irrelevant(self):
        store(self.run / "execution.json", {"request": {"expires_at": 0}})
        with patch.object(caller, "_verify_context", side_effect=AssertionError("Execution revalidation")):
            result = self.inspect()
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(result["command_success"], "known_success")
        self.assertEqual(result["commands"][0]["receipt_check"], "matched")
        self.assertNotIn("result_text", result)
        self.assertEqual(result["commands"][0]["command"], self.command)

    def test_tampered_artifact_and_terminal_identity(self):
        (self.run / "core.jsonl").write_text("tampered")
        self.assertEqual(self.inspect()["first_blocker"], "ARTIFACT_HASH_MISMATCH")
        self.publish()
        self.terminal["native_thread_id"] = "87654321-1234-1234-1234-123456789abc"
        store(self.run / "terminal.json", self.terminal)
        self.assertEqual(self.inspect()["first_blocker"], "THREAD_ID_MISMATCH")

    def test_event_and_command_mismatch(self):
        evidence = json.loads((self.run / "command-evidence.json").read_text())
        evidence["records"][0]["event_sha256"] = "0" * 64
        store(self.run / "command-evidence.json", evidence)
        self.terminal["command_evidence_artifact"] = ref(self.run / "command-evidence.json")
        store(self.run / "terminal.json", self.terminal)
        self.assertEqual(self.inspect()["first_blocker"], "EVIDENCE_IDENTITY_MISMATCH")
        evidence["records"][0]["event_sha256"] = caller._canonical_sha256(self.events[0])
        evidence["records"][0]["command_sha256"] = "0" * 64
        store(self.run / "command-evidence.json", evidence)
        self.terminal["command_evidence_artifact"] = ref(self.run / "command-evidence.json")
        store(self.run / "terminal.json", self.terminal)
        self.assertEqual(self.inspect()["first_blocker"], "EVIDENCE_IDENTITY_MISMATCH")

    def test_nonzero_unknown_and_conflict(self):
        self.item["exit_code"] = 7
        self.item["aggregated_output"]["exit_code"] = 7
        self.receipt.update(exit_code=7, terminal_state="failed")
        store(self.receipt_path, self.receipt)
        self.publish()
        self.assertEqual(self.inspect()["commands"][0]["execution_proof"], "known_failure")
        self.item["exit_code"] = None
        self.item["aggregated_output"] = {"exit_code": None}
        self.publish()
        result = self.inspect()
        self.assertEqual(result["commands"][0]["execution_proof"], "unavailable")
        self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
        self.item["exit_code"] = 0
        self.item["aggregated_output"]["exit_code"] = 7
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "EXIT_CODE_CONFLICT")

    def test_known_failed_source_read_receipt_preserves_failure_and_terminal(self):
        self.item.update(command="source_read a.py", command_type="source_read", status="failed",
                         exit_code=1, success=False)
        self.item["aggregated_output"].update(exit_code=1, error="SOURCE_READ_RANGE_OUT_OF_BOUNDS")
        self.receipt.update(exit_code=1, terminal_state="failed", authority_effect="none")
        store(self.receipt_path, self.receipt)
        self.events[1]["status"] = "failed"
        self.publish()
        self.terminal.update(status="BLOCKED", first_typed_blocker="ORIGINAL_FAILURE")
        store(self.run / "terminal.json", self.terminal)
        before = {p: p.read_bytes() for p in (self.run / "terminal.json", self.receipt_path)}
        result = self.inspect(command_stream="priority")
        self.assertEqual(result["artifact_integrity"], "verified")
        self.assertEqual(result["status"], "INCOMPLETE_EVIDENCE")
        self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
        self.assertEqual(result["command_success"], "known_failure")
        self.assertEqual(result["terminal_status"], "BLOCKED")
        self.assertEqual(result["terminal_first_blocker"], "ORIGINAL_FAILURE")
        row = result["commands"][0]
        self.assertEqual((row["receipt_check"], row["exit_code"], row["execution_proof"]),
                         ("matched", 1, "known_failure"))
        self.assertEqual(row["diagnostic_excerpt"], "SOURCE_READ_RANGE_OUT_OF_BOUNDS")
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                patch.object(inspector, "inspect", wraps=inspector.inspect) as inspected:
            projected = inspector.project_terminal(self.terminal, self.root, RID)
        inspected.assert_called_once_with(self.root, RID, THREAD, command_stream="priority")
        with patch.object(inspector, "inspect", side_effect=AssertionError("no second inspection")), \
                patch.object(Path, "open", side_effect=AssertionError("no artifact reads")):
            summary = inspector.summarize_terminal(projected, self.root, RID)
        for displayed in (projected, summary):
            self.assertEqual(displayed["status"], "BLOCKED")
            self.assertEqual(displayed["first_typed_blocker"], "ORIGINAL_FAILURE")
            compact = displayed["result_inspection"]
            self.assertEqual(compact["status"], "INCOMPLETE_EVIDENCE")
            self.assertEqual(compact["first_blocker"], "COMMAND_FAILURE")
            self.assertEqual(compact["command_success"], "known_failure")
            self.assertEqual(compact["cleanup"], result["cleanup"])
            self.assertEqual(compact["pagination"], result["pagination"])
            self.assertEqual(tuple(compact["commands"][0][key] for key in
                                   ("receipt_check", "exit_code", "execution_proof", "diagnostic_excerpt")),
                             ("matched", 1, "known_failure", "SOURCE_READ_RANGE_OUT_OF_BOUNDS"))
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_projection_omits_general_diagnostics_without_known_failure(self):
        baseline = self.inspect(command_stream="priority")
        success_proof = baseline["commands"][0]["execution_proof"]
        cases = ((success_proof, "SOURCE_BODY_NOT_FOR_PROJECTION"),
                 ("unavailable", "SOURCE_BODY_NOT_FOR_PROJECTION"),
                 ("known_failure", ""), ("known_failure", None),
                 ("known_failure", 1), ("known_failure", {"stdout": "not a diagnostic"}))
        for proof, diagnostic in cases:
            with self.subTest(proof=proof, diagnostic=diagnostic):
                evidence = deepcopy(baseline)
                evidence["commands"][0].update(execution_proof=proof, diagnostic_excerpt=diagnostic)
                with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                        patch.object(inspector, "inspect", return_value=evidence), \
                        patch.object(Path, "open", side_effect=AssertionError("no artifact reads")):
                    projected = inspector.project_terminal(self.terminal, self.root, RID)
                self.assertNotIn("diagnostic_excerpt", projected["result_inspection"]["commands"][0])
                self.assertNotIn("SOURCE_BODY_NOT_FOR_PROJECTION", json.dumps(projected))

    def test_projection_preserves_verifier_diagnostic_precedence(self):
        baseline = self.inspect(command_stream="priority")
        for proof in (baseline["commands"][0]["execution_proof"], "unavailable", "known_failure"):
            with self.subTest(proof=proof):
                evidence = deepcopy(baseline)
                evidence["commands"][0].update(execution_proof=proof,
                                               diagnostic_excerpt="GENERAL_DIAGNOSTIC",
                                               verifier_output_excerpt="VERIFIER_DIAGNOSTIC")
                with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                        patch.object(inspector, "inspect", return_value=evidence):
                    projected = inspector.project_terminal(self.terminal, self.root, RID)
                self.assertEqual(projected["result_inspection"]["commands"][0]["diagnostic_excerpt"],
                                 "VERIFIER_DIAGNOSTIC")

    def test_failed_projection_diagnostic_is_utf8_bounded(self):
        baseline = self.inspect(command_stream="priority")
        limit = inspector.MAX_DIAGNOSTIC_BYTES
        for diagnostic, expected in (("é" * limit, "é" * (limit // 2)),
                                     ("a" * (limit - 1) + "é", "a" * (limit - 1)),
                                     ("\ud800", "?")):
            with self.subTest(diagnostic=diagnostic):
                evidence = deepcopy(baseline)
                evidence["commands"][0].update(execution_proof="known_failure",
                                               diagnostic_excerpt=diagnostic)
                with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                        patch.object(inspector, "inspect", return_value=evidence):
                    projected = inspector.project_terminal(self.terminal, self.root, RID)
                excerpt = projected["result_inspection"]["commands"][0]["diagnostic_excerpt"]
                self.assertEqual(excerpt, expected)
                self.assertTrue(excerpt)
                self.assertLessEqual(len(excerpt.encode("utf-8")), limit)
                self.assertLessEqual(len(caller._canonical_bytes(projected)), inspector.MAX_PROJECTED_BYTES)

    def test_failure_diagnostics_obey_existing_projection_budget_and_pagination(self):
        evidence = self.inspect(command_stream="priority")
        evidence.update(status="INCOMPLETE_EVIDENCE", first_blocker="COMMAND_FAILURE",
                        command_success="known_failure")
        row = evidence["commands"][0]
        evidence["commands"] = [
            {**row, "event_index": index, "command": "x" * inspector.MAX_COMMAND_BYTES,
             "exit_code": 1, "execution_proof": "known_failure",
             "diagnostic_excerpt": "é" * inspector.MAX_DIAGNOSTIC_BYTES}
            for index in range(inspector.PAGE_SIZE)
        ]
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                patch.object(Path, "open", side_effect=AssertionError("no artifact reads")):
            with patch.object(inspector, "inspect", return_value=deepcopy(evidence)):
                full = inspector.project_terminal(self.terminal, self.root, RID)
            self.assertEqual(len(full["result_inspection"]["commands"]), inspector.PAGE_SIZE)
            limit = len(caller._canonical_bytes(full)) - 1
            self.assertLess(limit, inspector.MAX_PROJECTED_BYTES)
            with patch.object(inspector, "inspect", return_value=deepcopy(evidence)), \
                    patch.object(inspector, "MAX_PROJECTED_BYTES", limit):
                bounded = inspector.project_terminal(self.terminal, self.root, RID)
        compact = bounded["result_inspection"]
        self.assertLessEqual(len(caller._canonical_bytes(bounded)), limit)
        self.assertEqual(compact["commands"], full["result_inspection"]["commands"][:-1])
        self.assertEqual(compact["pagination"], {**full["result_inspection"]["pagination"],
                                               "next_offset": inspector.PAGE_SIZE - 1})
        self.assertTrue(compact["projection_partial"])
        self.assertEqual(compact["projection_blocker"], "PROJECTION_PARTIAL")
        self.assertEqual((compact["status"], compact["first_blocker"], compact["command_success"]),
                         ("INCOMPLETE_EVIDENCE", "COMMAND_FAILURE", "known_failure"))
        self.assertEqual(compact["cleanup"], full["result_inspection"]["cleanup"])
        self.assertEqual({key: value for key, value in bounded.items() if key != "result_inspection"},
                         self.terminal)

    def test_receipt_terminal_state_must_agree_with_exit_code(self):
        for state, code in (("completed", 1), ("failed", 0), ("cancelled", 1), (None, 0)):
            with self.subTest(state=state, code=code):
                self.receipt.update(terminal_state=state, exit_code=code)
                self.item["exit_code"] = code
                self.item["aggregated_output"]["exit_code"] = code
                store(self.receipt_path, self.receipt)
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], "RECEIPT_PROOF_INVALID")
                self.assertEqual(result["command_success"], "unproven")

    def test_known_failed_receipt_requires_outcome_and_all_cleanup_proof(self):
        valid = {**self.receipt, "exit_code": 1, "terminal_state": "failed"}
        self.item["exit_code"] = self.item["aggregated_output"]["exit_code"] = 1
        cases = [("outcome", "unknown"), ("outcome", None)] + [
            (key, value) for key in ("process_reaped", "process_group_empty", "termination_proven")
            for value in (None, False, 1)]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.receipt.clear()
                self.receipt.update(valid)
                if value is None:
                    self.receipt.pop(key)
                else:
                    self.receipt[key] = value
                store(self.receipt_path, self.receipt)
                self.publish()
                self.assertEqual(self.inspect()["first_blocker"], "RECEIPT_PROOF_INVALID")

    def test_bool_receipt_exit_codes_rejected_in_either_copy(self):
        for actual_code, event_code in ((False, False), (True, True), (0, False),
                                        (1, True), (False, 0), (True, 1)):
            with self.subTest(actual=actual_code, event=event_code):
                code = int(event_code)
                self.receipt.update(exit_code=event_code,
                                    terminal_state="completed" if code == 0 else "failed")
                self.item["exit_code"] = self.item["aggregated_output"]["exit_code"] = code
                store(self.receipt_path, {**self.receipt, "exit_code": actual_code})
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], "RECEIPT_PROOF_INVALID")
                self.assertEqual(result["command_success"], "unproven")

    def test_failed_receipt_mismatch_and_conflicting_exit_codes_rejected(self):
        self.receipt.update(exit_code=1, terminal_state="failed")
        self.item["exit_code"] = self.item["aggregated_output"]["exit_code"] = 1
        store(self.receipt_path, {**self.receipt, "exit_code": 2})
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "RECEIPT_MISMATCH")
        store(self.receipt_path, self.receipt)
        for location in (self.item, self.item["aggregated_output"]):
            self.item["exit_code"] = self.item["aggregated_output"]["exit_code"] = 1
            location["exit_code"] = 2
            self.publish()
            self.assertEqual(self.inspect()["first_blocker"], "EXIT_CODE_CONFLICT")

    def test_completed_receipt_does_not_override_failed_command_status(self):
        self.item["status"] = "failed"
        self.publish()
        result = self.inspect()
        self.assertEqual(result["commands"][0]["receipt_check"], "matched")
        self.assertEqual(result["commands"][0]["execution_proof"], "unavailable")
        self.assertEqual(result["command_success"], "known_failure")
        self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
        self.assertEqual(result["status"], "INCOMPLETE_EVIDENCE")

    def test_missing_receipt_and_receipt_mismatch(self):
        self.item["aggregated_output"] = {"exit_code": 0}
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
        self.item["aggregated_output"] = {"exit_code": 0, "terminal_receipt": self.receipt,
                                           "terminal_receipt_path": str(self.receipt_path)}
        store(self.receipt_path, {**self.receipt, "exit_code": 3})
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "RECEIPT_MISMATCH")

    def test_partial_and_duplicate_index(self):
        self.events = [self.events[0].copy() for _ in range(full_core.MAX_COMMAND_EVIDENCE + 1)]
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "EVIDENCE_INDEX_PARTIAL")
        evidence = json.loads((self.run / "command-evidence.json").read_text())
        evidence["records"][1]["event_index"] = evidence["records"][0]["event_index"]
        store(self.run / "command-evidence.json", evidence)
        self.terminal["command_evidence_artifact"] = ref(self.run / "command-evidence.json")
        store(self.run / "terminal.json", self.terminal)
        self.assertEqual(self.inspect()["first_blocker"], "EVIDENCE_INDEX_MISMATCH")

    def test_path_escape_and_symlink(self):
        self.item["aggregated_output"]["terminal_receipt_path"] = str(self.root / "outside.json")
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "RECEIPT_PATH_INVALID")
        self.publish()
        (self.run / "supervision.json").unlink()
        (self.run / "supervision.json").symlink_to(self.root / "outside.json")
        self.assertEqual(self.inspect()["first_blocker"], "UNSAFE_FILE")

    def test_cleanup_observations(self):
        self.supervision["scope"]["engine_pid"] = 456
        self.supervision["scope"]["supervisor_pid"] = 789
        self.publish()
        with patch.object(inspector.os, "kill", side_effect=[None, PermissionError()]):
            result = self.inspect()
        self.assertEqual(result["cleanup"]["engine_pid"], "present_unowned")
        self.assertEqual(result["cleanup"]["supervisor_pid"], "permission_unknown")
        self.assertEqual(result["first_blocker"], "CLEANUP_UNPROVEN")

    def test_missing_pid_and_inconsistent_cleanup_never_pass(self):
        self.supervision["scope"].pop("engine_pid")
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "CLEANUP_UNPROVEN")
        self.supervision["scope"]["engine_pid"] = 456
        self.publish()
        self.terminal["cleanup"]["engine_pid"] = 999
        store(self.run / "terminal.json", self.terminal)
        self.assertEqual(self.inspect()["first_blocker"], "CLEANUP_UNPROVEN")

    def test_blocked_terminal_without_message_is_not_success(self):
        self.terminal["status"] = "BLOCKED"
        store(self.run / "terminal.json", self.terminal)
        self.assertEqual(self.inspect()["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        self.terminal["status"] = "RESULT_AVAILABLE"
        self.terminal["first_typed_blocker"] = "SOURCE_DRIFT"
        store(self.run / "terminal.json", self.terminal)
        result = self.inspect()
        self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        self.assertEqual(result["terminal_first_blocker"], "SOURCE_DRIFT")

    def test_no_command_is_not_claimed_as_command_success(self):
        self.events = []
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "EXECUTION_PROOF_UNAVAILABLE")

    def test_diagnostic_does_not_invent_execution(self):
        self.item["exit_code"] = None
        self.item["aggregated_output"] = "JSPACE_OPERATION_DENIED: operation=modify"
        self.publish()
        row = self.inspect()["commands"][0]
        self.assertEqual(row["diagnostic_excerpt"], self.item["aggregated_output"])
        self.assertEqual(row["execution_proof"], "unavailable")
        self.assertIsNone(row["exit_code"])

    def test_output_bytes_and_cursor_are_bounded(self):
        self.item["command"] = "\u5b57" * 680
        self.events = [self.events[0].copy() for _ in range(25)]
        self.publish()
        offset, seen = 0, []
        while True:
            result = self.inspect(offset=offset)
            self.assertLessEqual(len(caller._canonical_bytes(result)), inspector.MAX_PAGE_BYTES)
            seen.extend(row["event_index"] for row in result["commands"])
            next_offset = result["pagination"]["next_offset"]
            if next_offset is None:
                break
            self.assertGreater(next_offset, offset)
            offset = next_offset
        self.assertEqual(seen, list(range(25)))
        self.assertEqual(self.inspect(offset=26)["first_blocker"], "OFFSET_OUT_OF_RANGE")
        self.item["command"] = "x" * (inspector.MAX_COMMAND_BYTES + 1)
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "COMMAND_IDENTITY_OMITTED")

    def test_pagination_bounded(self):
        self.events = [self.events[0].copy() for _ in range(25)]
        self.publish()
        first = self.inspect()
        self.assertEqual(len(first["commands"]), inspector.PAGE_SIZE)
        second = self.inspect(offset=first["pagination"]["next_offset"])
        self.assertEqual(len(second["commands"]), 25 - inspector.PAGE_SIZE)
        self.assertIsNone(second["pagination"]["next_offset"])


    def terminal_evidence_fixture(self, *, requested=True, emit_marker=True, planning="Earlier planning, not final",
                                  terminal_status="done"):
        terminal, old_run, _ = self.capability_fixture(planning)
        original = json.loads((old_run / "request-identity.json").read_text())
        original = {key: value for key, value in original.items() if key not in {"request_id", "request_sha256"}}
        if requested:
            original["terminal_delivery"] = "evidence_only"
        digest = caller._canonical_sha256(original)
        original.update(request_id="tura_embedded_" + digest, request_sha256=digest)
        run = self.root / original["request_id"]
        run.mkdir(exist_ok=True)
        receipt_path = run / "execution-state/command_receipts/call-0.json"
        store(receipt_path, self.receipt)
        command = deepcopy(self.item)
        command["aggregated_output"]["terminal_receipt_path"] = str(receipt_path)
        marker = {"type": "nokiy.terminal_evidence", "schema_version": "nokiy_terminal_evidence_v1",
                  "session_id": "full-" + digest, "runtime_id": "fixture-runtime",
                  "terminal_status": terminal_status, "delivery_mode": "evidence_only",
                  "parent_acceptance_required": True, "final_summary_turn_executed": False}
        events = [{"type": "item.completed", "item": command},
                  {"type": "item.completed", "item": {"type": "assistant_message", "text": planning}}]
        if emit_marker:
            events.append(marker)
        events.append({"type": "turn.completed", "status": "completed", "usage": terminal["usage"],
                       **full_core._terminal_identity(original)})
        compact = {"terminal_status": terminal_status, "delivery_mode": "evidence_only",
                   "parent_acceptance_required": True, "final_summary_turn_executed": False}
        terminal.update(request_id=original["request_id"], request_sha256=digest,
                        result_text=caller._canonical_bytes(compact).decode() if emit_marker else planning,
                        result_truncated=False, result_artifact=None)
        if requested:
            terminal.update(requested_terminal_delivery="evidence_only",
                            observed_terminal_delivery="evidence_only" if emit_marker else "assistant_reply",
                            terminal_evidence=marker if emit_marker else None)
        store(run / "request-identity.json", original)
        store(run / "supervision.json", self.supervision)
        terminal["supervision_artifact"] = ref(run / "supervision.json")
        self.republish_terminal_evidence(terminal, run, events)
        return terminal, run, events

    def republish_terminal_evidence(self, terminal, run, events):
        trajectory = run / "core.jsonl"
        trajectory.write_bytes(b"".join(caller._canonical_bytes(event) + b"\n" for event in events))
        evidence = full_core._command_evidence(events)
        store(run / "command-evidence.json", evidence)
        terminal.update(trajectory_artifact=ref(trajectory),
                        command_evidence_artifact=ref(run / "command-evidence.json"),
                        command_evidence_summary={key: evidence[key] for key in ("total_count", "failed_count", "complete")})
        store(run / "terminal.json", terminal)

    def test_terminal_evidence_exact_recovery_and_summary_do_not_replay_or_reinspect(self):
        planning = self.capability_text(self.capability_handoff())
        terminal, run, _ = self.terminal_evidence_fixture(planning=planning)
        paths = [run / name for name in ("terminal.json", "core.jsonl", "request-identity.json",
                                         "supervision.json", "command-evidence.json")]
        before = {path: path.read_bytes() for path in paths}
        with patch.object(caller, "execute", side_effect=AssertionError("no replay")):
            projected = self.project_capability(terminal)
        self.assertEqual(projected["result_inspection"]["status"], "EVIDENCE_VERIFIED")
        self.assertIsNone(projected["result_inspection"]["first_blocker"])
        self.assertEqual(projected["result_inspection"]["command_success"], "known_success")
        self.assertEqual(projected["result_text"], terminal["result_text"])
        self.assertNotIn("Earlier planning", projected["result_text"])
        self.assertNotIn("capability_gap", projected)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        with patch.object(inspector, "inspect", side_effect=AssertionError("no second inspection")):
            summary = inspector.summarize_terminal(projected, self.root, terminal["request_id"])
        for key in full_core._TerminalEvidence.FIELD_NAMES:
            self.assertEqual(summary[key], terminal[key])
        self.assertEqual(summary["usage"], terminal["usage"])
        self.assertTrue(summary["cleanup_pass"])

    def test_blocked_terminal_evidence_retains_failure_or_falls_back_without_parent_context(self):
        for failed, blocker in ((False, "WORKER_TERMINAL_EVIDENCE_BLOCKED"), (True, "COMMAND_FAILURE")):
            def mutate(events, original, contract):
                if failed:
                    command = events[1]["item"]
                    command.update(status="failed", exit_code=1)
                    command["aggregated_output"].update(exit_code=1, stderr="original failure")
                    command["aggregated_output"]["terminal_receipt"].update(exit_code=1, terminal_state="failed")
            terminal, run, _ = self.parent_context_fixture(mutate, terminal_status="blocked")
            paths = [run / name for name in ("terminal.json", "core.jsonl", "request-identity.json",
                                             "supervision.json", "command-evidence.json", "file-change-evidence.json")]
            before = {path: path.read_bytes() for path in paths}
            with patch.object(caller, "execute", side_effect=AssertionError("no replay")), \
                    patch.object(file_change_evidence, "_postimage", side_effect=AssertionError("no source reread")):
                projected = self.project_capability(terminal)
            evidence = projected["result_inspection"]
            with self.subTest(failed=failed):
                self.assertEqual(evidence["status"], "INCOMPLETE_EVIDENCE")
                self.assertEqual(evidence["first_blocker"], blocker)
                self.assertEqual(evidence["command_success"], "known_failure" if failed else "known_success")
                self.assertEqual(evidence["counts"]["failed"], int(failed))
                self.assertEqual(evidence["mission_acceptance"], "parent_owned")
                self.assertNotIn("parent_review_context", evidence)
                self.assertIn("postimages", evidence["file_changes"])
                self.assertTrue(evidence["cleanup"]["historical_pass"])
                self.assertEqual((evidence["cleanup"]["engine_pid"], evidence["cleanup"]["supervisor_pid"]),
                                 ("absent", "absent"))
                if failed:
                    self.assertEqual((evidence["commands"][0]["exit_code"], evidence["commands"][0]["execution_proof"]),
                                     (1, "known_failure"))
                    self.assertEqual(evidence["commands"][0]["diagnostic_excerpt"], "original failure")
                    self.assertEqual(evidence["verifier_failure_resolution"]["unresolved_count"], 1)
                self.assertEqual(before, {path: path.read_bytes() for path in paths})
        with patch.object(inspector, "inspect", side_effect=AssertionError("no reinspection")), \
                patch.object(full_core, "_Trajectory", side_effect=AssertionError("no trajectory pass")):
            summary = inspector.summarize_terminal(projected, self.root, terminal["request_id"])
            reference = inspector.publish_inspection_summary(projected, self.root, terminal["request_id"],
                                                              expected_thread_id=THREAD)
            loaded = inspector.load_inspection_summary(reference, expected_request_id=terminal["request_id"],
                expected_thread_id=THREAD, terminal_reference={"path": str(run / "terminal.json"),
                                                               "sha256": summary["terminal_sha256"]})
        self.assertEqual(loaded, summary)
        self.assertEqual(summary["result_inspection"], evidence)
        for key in full_core._TerminalEvidence.FIELD_NAMES:
            self.assertEqual(summary[key], terminal[key])
        self.assertEqual(summary["usage"], terminal["usage"])
        self.assertTrue(summary["cleanup_pass"])
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_terminal_evidence_recovery_no_marker_falls_back_and_default_shape_is_unchanged(self):
        for requested in (True, False):
            terminal, _, _ = self.terminal_evidence_fixture(requested=requested, emit_marker=False)
            projected = self.project_capability(terminal)
            self.assertEqual(projected["result_inspection"]["status"], "EVIDENCE_VERIFIED")
            self.assertEqual(projected["result_text"], "Earlier planning, not final")
            summary = inspector.summarize_terminal(projected, self.root, terminal["request_id"])
            if requested:
                self.assertEqual(summary["observed_terminal_delivery"], "assistant_reply")
                self.assertIsNone(summary["terminal_evidence"])
            else:
                for key in full_core._TerminalEvidence.FIELD_NAMES:
                    self.assertNotIn(key, projected)
                    self.assertNotIn(key, summary)

    def test_terminal_evidence_recovery_rejects_invalid_unrequested_duplicate_and_late_markers(self):
        cases = ("foreign", "empty_runtime", "malformed", "bool_one", "bool_zero", "unknown_status",
                 "duplicate", "assistant", "tool", "unrequested")
        for case in cases:
            terminal, run, events = self.terminal_evidence_fixture(requested=case != "unrequested", terminal_status="blocked")
            events = deepcopy(events)
            if case == "foreign":
                events[2]["session_id"] = "full-foreign"
            elif case == "empty_runtime":
                events[2]["runtime_id"] = ""
            elif case == "malformed":
                events[2]["extra"] = "not allowed"
            elif case == "bool_one":
                events[2]["parent_acceptance_required"] = 1
            elif case == "bool_zero":
                events[2]["final_summary_turn_executed"] = 0
            elif case == "unknown_status":
                events[2]["terminal_status"] = "unknown"
            elif case == "duplicate":
                events.insert(3, deepcopy(events[2]))
            elif case in {"assistant", "tool"}:
                events.insert(3, {"type": "item.completed", "item": {"type": "assistant_message" if case == "assistant"
                                                                 else "command_execution", "text": "late"}})
            self.republish_terminal_evidence(terminal, run, events)
            with self.subTest(case=case):
                projected = self.project_capability(terminal)
                self.assertEqual(projected["result_inspection"]["first_blocker"], "NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID")
                self.assertNotEqual(projected["result_inspection"]["status"], "EVIDENCE_VERIFIED")

    def test_terminal_evidence_recovery_rejects_relabeling_and_readback_type_drift(self):
        for case in ("prose", "observed", "requested", "missing", "status", "marker_one", "marker_zero"):
            terminal, run, _ = self.terminal_evidence_fixture(terminal_status="blocked")
            if case == "prose":
                terminal["result_text"] = "Earlier planning, not final"
            elif case == "observed":
                terminal["observed_terminal_delivery"] = "assistant_reply"
            elif case == "requested":
                terminal["requested_terminal_delivery"] = "assistant_reply"
            elif case == "missing":
                terminal.pop("terminal_evidence")
            elif case == "status":
                terminal["terminal_evidence"] = dict(terminal["terminal_evidence"], terminal_status="done")
            else:
                terminal["terminal_evidence"] = dict(terminal["terminal_evidence"])
                key = "parent_acceptance_required" if case == "marker_one" else "final_summary_turn_executed"
                terminal["terminal_evidence"][key] = 1 if case == "marker_one" else 0
            store(run / "terminal.json", terminal)
            with self.subTest(case=case):
                projected = self.project_capability(terminal)
                expected = "NOKIY_FULL_CORE_RESULT_INVALID" if case == "prose" else "NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID"
                self.assertEqual(projected["result_inspection"]["first_blocker"], expected)
                self.assertNotEqual(projected["result_inspection"]["status"], "EVIDENCE_VERIFIED")


if __name__ == "__main__":
    unittest.main()
