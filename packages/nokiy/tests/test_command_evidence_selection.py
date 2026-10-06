"""Exact evidence selection; no model execution."""
import hashlib
import io
import json
import sys
from contextlib import redirect_stdout
from unittest.mock import patch
import test_result_inspection as fixture
from codex_collaboration_harness import result_inspection as inspector


class CommandSelectionTests(fixture.InspectionTests):
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
