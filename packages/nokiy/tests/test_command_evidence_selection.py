"""Exact evidence selection; no model execution."""
import hashlib
import io
import json
import sys
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
# Flat sibling imports like the other suites: a site-packages "tests" package must not shadow them.
import test_result_inspection as fixture
from test_result_inspection import RID, THREAD, store
from codex_collaboration_harness import embedded_nokiy as caller, full_core, result_inspection as inspector


class CommandSelectionTests(fixture.InspectionTests):
    def delivery_request(self, index=1):
        request = super().delivery_request(index)
        # Batch preimage capture now reads the contract even for members without writes.
        contract = {"schema_version": "jspace_contract_v2", "repo_root": str(request.workspace),
                    "read_scopes": [], "write_scopes": [], "declared_targets": [],
                    "allowed_operations": [], "denied_operations": [], "command_templates": [],
                    "command_effect_policy": "trusted_argv_effects_v1",
                    "expansion": {"mode": "exact_target_only", "error_code": "JSPACE_EXPANSION_REQUIRED",
                                  "mutation_on_expansion": False}}
        path = request.artifact_root / "jspace.json"
        store(path, contract)
        request.jspace_contract = caller.FileIdentity(path=path, sha256=caller._canonical_sha256(contract))
        return request

    def digest(self, value=None):
        return hashlib.sha256((value or self.command).encode()).hexdigest()

    def many(self, count=40):
        self.events = [{"type": "item.completed", "item": {
            **self.item, "command": f"read-{index}"}} for index in range(count)]
        self.events.append({"type": "item.completed", "item": self.item})
        self.publish()

    def test_exact_selection_returns_last_command_in_one_page(self):
        self.many()
        original = (self.run / "terminal.json").read_bytes()
        result = self.inspect(command_sha256=(self.digest(),))
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        self.assertEqual(result["counts"]["commands"], 41)
        self.assertEqual([row["event_index"] for row in result["commands"]], [40])
        self.assertEqual(result["command_selection"]["matches"], {self.digest(): 1})
        self.assertIsNone(result["pagination"]["next_offset"])
        self.assertEqual((self.run / "terminal.json").read_bytes(), original)

    def test_unselected_failed_command_still_blocks(self):
        self.many()
        self.events[0]["item"] = {**self.item, "command": "failed", "exit_code": 1,
                                 "aggregated_output": {"exit_code": 1}}
        self.publish()
        result = self.inspect(command_sha256=(self.digest(),))
        self.assertEqual(result["first_blocker"], "COMMAND_FAILURE")
        self.assertEqual(result["status"], "INCOMPLETE_EVIDENCE")
        self.assertEqual(result["commands"][0]["exit_code"], 0)

    def test_unselected_unknown_proof_still_blocks(self):
        self.many()
        self.events[0]["item"] = {**self.item, "command": "unknown",
                                 "aggregated_output": {"exit_code": 0}}
        self.publish()
        self.assertEqual(self.inspect(command_sha256=(self.digest(),))["first_blocker"],
                         "EXECUTION_PROOF_UNAVAILABLE")

    def test_missing_exact_command_is_not_success(self):
        result = self.inspect(command_sha256=(self.digest("not executed"),))
        self.assertEqual(result["first_blocker"], "REQUESTED_COMMAND_NOT_FOUND")
        self.assertEqual(result["commands"], [])
        self.assertEqual(result["command_selection"]["missing"], [self.digest("not executed")])
        self.assertEqual(result["command_success"], "known_success")

    def test_missing_selection_cannot_replace_integrity_failure(self):
        (self.run / "core.jsonl").write_text("tampered")
        result = self.inspect(command_sha256=(self.digest("missing"),))
        self.assertEqual(result["first_blocker"], "ARTIFACT_HASH_MISMATCH")

    def test_duplicate_execution_remains_visible_and_paginated(self):
        self.events = [{"type": "item.completed", "item": self.item}
                       for _ in range(inspector.PAGE_SIZE + 3)]
        self.publish()
        first = self.inspect(command_sha256=(self.digest(),))
        self.assertEqual(len(first["commands"]), inspector.PAGE_SIZE)
        self.assertEqual(first["command_selection"]["matches"][self.digest()], 19)
        second = self.inspect(command_sha256=(self.digest(),),
                              offset=first["pagination"]["next_offset"])
        self.assertEqual(len(second["commands"]), 3)
        self.assertEqual(second["commands"][0]["event_index"], 16)
        self.assertIsNone(second["pagination"]["next_offset"])
        self.assertEqual(self.inspect(command_sha256=(self.digest(),), offset=20)["first_blocker"],
                         "OFFSET_OUT_OF_RANGE")

    def test_invalid_or_unbounded_selection_rejected_before_read(self):
        for value in ("a" * 64, ("x",), ("A" * 64,), ([1],),
                      (self.digest(), self.digest()), tuple(f"{i:064x}" for i in range(9))):
            with self.subTest(value=value), patch.object(inspector, "_directory") as read:
                result = self.inspect(command_sha256=value)
            self.assertEqual(result["first_blocker"], "COMMAND_SELECTION_INVALID")
            read.assert_not_called()

    def test_cli_exposes_selection(self):
        self.many()
        argv = ["inspect", "--artifact-root", str(self.root),
                "--request-id", self.run.name, "--expected-thread-id",
                self.terminal["native_thread_id"], "--command-sha256", self.digest()]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            code = inspector.main()
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output.getvalue())["commands"]), 1)

    # Keep inherited coverage current without editing the shared inspection module.
    # Synthetic execution uses fixture receipts and has no inherited native process scope.
    def test_actual_batch_execute_one_publishes_all_five_once_and_compact_references_fit(self):
        with patch("codex_collaboration_harness.graph_process.inherited_root_parent", return_value=None):
            super().test_actual_batch_execute_one_publishes_all_five_once_and_compact_references_fit()

    def test_batch_publication_failure_keeps_result_and_result_only_recovery_does_not_write(self):
        with patch("codex_collaboration_harness.graph_process.inherited_root_parent", return_value=None):
            super().test_batch_publication_failure_keeps_result_and_result_only_recovery_does_not_write()

    def test_capability_gap_obeys_projection_budget_without_overwriting_actual_blocker(self):
        terminal, run, _ = self.capability_fixture(self.capability_text(self.capability_handoff()))
        terminal.update(status="BLOCKED", first_typed_blocker="ORIGINAL_SCOPE_DENIED")
        store(run / "terminal.json", terminal)
        projected = self.project_capability(terminal)
        baseline = {key: value for key, value in projected.items() if key != "capability_gap"}
        # Projection reserves space for the metadata appended by read-only recovery.
        recovery = {"inspection_summary": None, "inspection_summary_omission": "READ_ONLY_RECOVERY"}
        limit = (len(caller._canonical_bytes(baseline)) + 32 +
                 len(caller._canonical_bytes(inspector._gap_diagnostic("CAPABILITY_GAP_PROJECTION_TOO_LARGE"))) +
                 len(caller._canonical_bytes(recovery)) - 1)
        with patch.object(inspector, "MAX_PROJECTED_BYTES", limit):
            bounded = self.project_capability(terminal)
        self.assertLessEqual(len(caller._canonical_bytes(bounded)), limit)
        self.assertLessEqual(len(caller._canonical_bytes({**bounded, **recovery})), limit)
        self.assertEqual(bounded["capability_gap"]["first_blocker"], "CAPABILITY_GAP_PROJECTION_TOO_LARGE")
        self.assertEqual(bounded["status"], "BLOCKED")
        self.assertEqual(bounded["first_typed_blocker"], "ORIGINAL_SCOPE_DENIED")
        self.assertEqual(bounded["result_inspection"]["first_blocker"], projected["result_inspection"]["first_blocker"])

    def test_output_bytes_and_cursor_are_bounded(self):
        self.item["command"] = "\u5b57" * 680
        self.events = [self.events[0].copy() for _ in range(25)]
        self.publish()
        offset, seen = 0, []
        while True:
            result = self.inspect(offset=offset)
            self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
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
        result = self.inspect()
        # The body is bounded; its verified identity and execution proof remain available.
        self.assertEqual(result["status"], "EVIDENCE_VERIFIED")
        self.assertIsNone(result["first_blocker"])
        self.assertEqual(result["command_success"], "known_success")
        self.assertLessEqual(len(caller._canonical_bytes(result)), inspector.MAX_PAGE_BYTES)
        self.assertEqual(len(result["commands"]), inspector.PAGE_SIZE)
        for row in result["commands"]:
            self.assertIsNone(row["command"])
            self.assertEqual(row["command_head"], "x" * inspector.MAX_COMMAND_HEAD_CHARS)
            self.assertEqual(row["command_bytes"], inspector.MAX_COMMAND_BYTES + 1)
            self.assertEqual(row["command_sha256"], self.digest(self.item["command"]))
            self.assertEqual(row["execution_proof"], "known_success")

    def test_summary_runtime_diagnostics_are_exact_optional_and_pure(self):
        projected = self.project_capability(self.terminal)
        baseline = inspector.summarize_terminal(projected, self.root, RID)
        diagnostics = self.runtime_diagnostics()
        variants = ({}, diagnostics, {key: None for key in diagnostics},
                    {key: {} for key in diagnostics},
                    *({key: value} for key, value in diagnostics.items()))
        with ExitStack() as stack:
            for target, name in ((inspector, "inspect"), (inspector, "_file"),
                                 (inspector.os, "open"), (inspector.os, "kill"), (Path, "open"),
                                 (caller, "read_terminal"), (full_core, "_ArtifactReader"),
                                 (caller, "_canonical_bytes"), (caller, "_canonical_sha256"),
                                 (hashlib, "sha256"), (caller, "execute")):
                stack.enter_context(patch.object(target, name,
                    side_effect=AssertionError("summary must only select already projected fields")))
            for optional in variants:
                with self.subTest(diagnostics=optional):
                    candidate = {**projected, **optional}
                    before = deepcopy(candidate)
                    summary = inspector.summarize_terminal(candidate, self.root, RID)
                    retained = {"tool_loop": optional["tool_loop"]} if "tool_loop" in optional else {}
                    self.assertEqual(summary, {**baseline, **retained})
                    self.assertEqual(candidate, before)
                    for key in diagnostics:
                        if key == "tool_loop" and key in optional:
                            self.assertIs(summary[key], candidate[key])
                        else:
                            self.assertNotIn(key, summary)

    def test_retained_summary_roundtrip_inspects_once_and_reuses_only_identical_bytes(self):
        diagnostics = self.runtime_diagnostics()
        self.terminal.update(diagnostics)
        store(self.run / "terminal.json", self.terminal)
        terminal_before = (self.run / "terminal.json").read_bytes()
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}), \
                patch.object(inspector, "inspect", wraps=inspector.inspect) as inspected:
            projected = inspector.project_terminal(self.terminal, self.root, RID)
            original = deepcopy(projected)
            with patch.object(inspector.os, "kill", side_effect=AssertionError("no publication process pass")), \
                    patch.object(full_core, "_ArtifactReader", side_effect=AssertionError("no second trajectory pass")), \
                    patch.object(caller, "read_terminal", side_effect=AssertionError("no terminal reread")), \
                    patch.object(inspector, "_file", wraps=inspector._file) as summary_reads:
                delivered = inspector.retain_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
                reference = delivered["inspection_summary"]
                path = self.run / inspector.INSPECTION_SUMMARY_FILE
                raw, metadata = path.read_bytes(), path.stat()
                again = inspector.publish_inspection_summary(projected, self.root, RID, expected_thread_id=THREAD)
                loaded = self.load_summary(reference)
        inspected.assert_called_once_with(self.root, RID, THREAD, command_stream="priority")
        self.assertEqual(len(summary_reads.call_args_list), 3)  # Publication, byte reuse, snapshot load only.
        for call in summary_reads.call_args_list:
            self.assertEqual(call.args, (Path(inspector.INSPECTION_SUMMARY_FILE), inspector.MAX_PROJECTED_BYTES))
        for key, value in diagnostics.items():
            self.assertEqual(projected[key], value)
            if key == "tool_loop":
                self.assertEqual(loaded[key], value)
            else:
                self.assertNotIn(key, loaded)  # Phase detail stays in the unchanged terminal.
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
