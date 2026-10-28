# SPDX-License-Identifier: MIT
"""Opt-in semantic cutover regression using real binaries and scripted SSE.

Set NOKIY_CUTOVER_STAGE_MANIFEST plus the image variables documented in
test_responses_evidence_handoff.py. Frozen source is reused, never historical
execution authority or model usage. Every scenario uses a fresh workspace.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest

import test_responses_evidence_handoff as handoff


STAGE_ENV = "NOKIY_CUTOVER_STAGE_MANIFEST"
READBACK_ENV = "NOKIY_CUTOVER_READBACK_DIR"
STAGE_SHA = "b9b931ab6dde08393362fc756fc3c1975a77637e86dcd0697ff688e8d20c642f"
GOLDEN = {
    "src/runtime_cutover/coordinator.py":
        "f7f3c89b25b2aac7c82b42b377fc698e1d167fe0d57c67b021fe3215a85236b2",
    "src/runtime_cutover/ledger.py":
        "a7d31aca4cbaa69b0f852f54da1cb1de074622fd71a940ef4d5a93a27828a280",
    "src/runtime_cutover/owners.py":
        "f31adf372e26b4fbe5294df27acb225fc4bb0c389fa5b92be19b421fd6df34c0",
}
FAILED_BEFORE_START = {
    "attempt": 1, "status": "failed_before_start", "execution_started": False,
    "test_process_count": 0, "mutation_count": 0, "retryable": True,
}


def _tree_rows(root):
    return [{"path": str(path.relative_to(root)), "sha256": handoff.file_sha256(path),
             "size": path.stat().st_size}
            for path in sorted(root.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts]


class CutoverRuntimeHandoffTests(unittest.TestCase):
    def setUp(self):
        selected = os.environ.get(STAGE_ENV)
        if not selected:
            self.skipTest(f"set {STAGE_ENV} to the frozen cutover manifest")
        self.stage_path = Path(selected).resolve(strict=True)
        self.assertEqual(handoff.file_sha256(self.stage_path), STAGE_SHA)
        self.stage = json.loads(self.stage_path.read_text())
        self.template = Path(self.stage["fixture"]["template_root"])
        self.before = _tree_rows(self.template)
        self.assertFalse(any(p.is_symlink() for p in self.template.rglob("*")))
        tree_sha = hashlib.sha256(json.dumps(
            self.before, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(tree_sha, self.stage["fixture"]["template_tree_sha256"])
        self.oracle = Path(self.stage["fixture"]["outcome_verifier"]["path"])
        self.assertEqual(handoff.file_sha256(self.oracle),
                         self.stage["fixture"]["outcome_verifier"]["sha256"])
        self.golden_root = Path(self.stage["arms"]["FULL"]["workspace"])
        for relative, digest in GOLDEN.items():
            self.assertEqual(handoff.file_sha256(self.golden_root / relative), digest)
        self.assertEqual(set(self.stage["fixture"]["editable_paths"]), set(GOLDEN))
        self.output = os.environ.get(READBACK_ENV)
        self.h = handoff.RuntimeEvidenceHandoffTests()
        self.addCleanup(self.h.doCleanups)
        self.h.setUp()
        self.f = self.h.f
        shutil.copytree(self.template, self.f.workspace, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.assertEqual(_tree_rows(self.f.workspace), self.before)
        self.scratch = self.f.artifacts / "verifier"
        self.scratch.mkdir()
        # The historical fixture writes workspace/artifacts. The current verifier
        # contract requires external scratch; relocate only its receipt paths.
        self.adapter = self.f.workspace / "tools/nokiy_verifier.py"
        self.adapter.write_text(
            "import runpy, sys\nfrom pathlib import Path\n"
            "module = runpy.run_path(sys.argv[1])\n"
            f"scratch = Path({str(self.scratch)!r})\n"
            "module['main'].__globals__.update(ARTIFACTS=scratch, "
            "ATTEMPTS=scratch/'verification_attempts.json', RECEIPT=scratch/'verify_receipt.json')\n"
            "raise SystemExit(module['main']())\n")
        self.workspace_before = _tree_rows(self.f.workspace)
        self.oracle_copy = self.f.root / "outcome_verifier.py"
        shutil.copy2(self.oracle, self.oracle_copy)
        self.oracle_adapter = self.f.root / "oracle_adapter.py"
        self.oracle_adapter.write_text(
            "import json, runpy, sys\nfrom pathlib import Path\n"
            "module = runpy.run_path(sys.argv[1])\n"
            "def attempts(workspace):\n"
            "    path = Path(sys.argv[3]) / 'verification_attempts.json'\n"
            "    return json.loads(path.read_text()) if path.is_file() else []\n"
            "module['evaluate'].__globals__['_load_attempts'] = attempts\n"
            "result = module['evaluate'](Path(sys.argv[2]))\n"
            "print(json.dumps(result))\n"
            "raise SystemExit(0 if result['verified_completion'] else 1)\n")
        self.addCleanup(self._assert_frozen_inputs)

    def _assert_frozen_inputs(self):
        self.assertEqual(_tree_rows(self.template), self.before)
        self.assertEqual(handoff.file_sha256(self.stage_path), STAGE_SHA)
        self.assertEqual(handoff.file_sha256(self.oracle),
                         self.stage["fixture"]["outcome_verifier"]["sha256"])
        for relative, digest in GOLDEN.items():
            self.assertEqual(handoff.file_sha256(self.golden_root / relative), digest)

    def _oracle(self):
        result = subprocess.run(
            [sys.executable, "-B", str(self.oracle_adapter), str(self.oracle_copy),
             str(self.f.workspace), str(self.scratch)],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
        self.assertIn(result.returncode, (0, 1), result.stderr[-2000:])
        return json.loads(result.stdout)

    def _request(self, base_url):
        self.h._loopback_image(base_url)
        python = Path(sys.executable).resolve()
        verifier = self.f.workspace / "tools/verify.py"
        action = {
            "mission": {"mission_id": "frozen-cutover-recovery", "task_id": "semantic-repair",
                        "mode": "DELIVERY", "objective": "Verify frozen cutover repair and recovery",
                        "current_predicate": "semantic repair and exact recovery unverified"},
            "context_summary": "Disposable frozen fixture; scripted provider; parent owns acceptance.",
            "operations": ["read", "command", "modify"], "source_read": True,
            "read_scopes": [row["path"] for row in self.workspace_before],
            "write_scopes": list(GOLDEN), "target_paths": list(GOLDEN),
            "source_sections": [{"path": relative, "start_line": 1,
                                 "end_line": len((self.template / relative).read_text().splitlines())}
                                for relative in GOLDEN],
            "command_templates": [], "forbidden_effects": ["network", "delete"],
            "verifier_commands": [{"argv": [str(python), "-B", str(self.adapter), str(verifier)],
                "executable_sha256": handoff.file_sha256(python),
                "pinned_files": [{"path": str(self.adapter),
                                  "sha256": handoff.file_sha256(self.adapter)}],
                "timeout_seconds": 30, "scratch_root": str(self.scratch), "network": False}],
        }
        capsule, self.h.contract = handoff.local_context.compile_context(
            self.f.workspace, action, artifact_root=self.f.artifacts)
        self.f.context.write_text(json.dumps(capsule))
        self.f.jspace.write_text(json.dumps(self.h.contract))
        path = self.f._write_request(prompt=(
            "Apply the frozen three-file repair. Inspect failed-before-start verification evidence "
            "before retrying once. Finish only after the admitted verifier passes."))
        value = json.loads(path.read_text())
        value.update(execution_profile="direct", authority_effect="workspace", model=handoff.MODEL,
                     reasoning_effort="max", service_tier="default", terminal_delivery="evidence_only",
                     timeout_seconds=120, max_trajectory_bytes=4 * 1024 * 1024, max_result_bytes=1024,
                     initial_task_state={"task_group": "cutover regression", "task_type": ["debug"]})
        path.write_text(json.dumps(value))
        return handoff.caller.load_request(path)

    def _messages(self):
        commands = []
        for relative in GOLDEN:
            old = (self.template / relative).read_text().splitlines(keepends=True)
            new = (self.golden_root / relative).read_text().splitlines(keepends=True)
            delta = (f"*** Begin Patch\n*** Update File: {relative}\n@@\n"
                     + "".join("-" + line for line in old)
                     + "".join("+" + line for line in new) + "*** End Patch\n")
            commands.append(handoff._command("apply_patch", delta, 1))
        verifier = handoff._command("focused_verifier", '{"verifier_index":0}', 2)
        done = handoff._command("task_status", '{"status":"done"}', 3)

        def safe_retry(body):
            # The second response depends on real evidence, not a timer or a
            # predeclared mock success. No extra source reread is needed.
            attempts = json.loads((self.scratch / "verification_attempts.json").read_text())
            self.assertEqual(attempts, [FAILED_BEFORE_START])
            self.assertIn("failed_before_start", json.dumps(body))
            return handoff._tool_message([verifier, done], 1)

        return [handoff._tool_message(commands + [verifier, done], 0), safe_retry]

    def _execute(self, *, disconnect_before=False, disconnect_after=False):
        baseline = self._oracle()
        self.assertEqual(baseline["status"], "FAIL")
        self.assertFalse(baseline["metrics"]["public_suite_pass"])
        messages = self._messages()
        if disconnect_before:
            messages.insert(0, None)

        def observe_effects():
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                receipt = self.scratch / "verification_attempts.json"
                if receipt.is_file() and all(handoff.file_sha256(self.f.workspace / path) == digest
                                             for path, digest in GOLDEN.items()):
                    self.assertEqual(json.loads(receipt.read_text()), [FAILED_BEFORE_START])
                    return
                time.sleep(0.01)  # Wait on observable effects, not a simulated workload.
            raise AssertionError("real effects not observed before stream interruption")

        with handoff._loopback_provider(
                messages, stream_gate=observe_effects if disconnect_after else None,
                drop_terminal=disconnect_after) as (url, calls, errors, bodies), \
                handoff._offline_runtime_environment(self.h.native_environment, url,
                                                    zero_backoff=True) as fences:
            request = self._request(url)
            ready = handoff.caller.preflight(request)
            self.assertEqual(ready["status"], "READY", ready)
            self.assertEqual(calls, [])
            terminal = handoff.caller.execute(request)
            run = request.artifact_root / request.request_id
            events = [json.loads(line) for line in (run / "core.jsonl").read_text().splitlines()]
            inspected = handoff.inspector.inspect(
                request.artifact_root, request.request_id, request.native_thread_id)
            prefix = None
            if disconnect_after:
                session_id = "full-" + request.request_sha256
                root = run / "execution-state/session_log"
                with closing(sqlite3.connect((root / "index.sqlite3").as_uri() + "?mode=ro",
                                             uri=True)) as db:
                    paths = db.execute("SELECT workspace_db_path FROM runtime_locations "
                                       "WHERE session_id = ?", (session_id,)).fetchall()
                self.assertEqual(len(paths), 1)
                path = Path(paths[0][0])
                self.assertTrue(path.is_relative_to(root) and not path.is_symlink())
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    prefix = {"runtime_events": [json.loads(row[0]) for row in db.execute(
                        "SELECT event_json FROM runtime_events ORDER BY rowid")],
                        "context_records": [json.loads(row[0]) for row in db.execute(
                        "SELECT record_json FROM session_context_records WHERE session_id = ? "
                        "ORDER BY sequence", (session_id,))]}
                prefix["command_receipts"] = [json.loads(path.read_text()) for path in sorted(
                    (run / "execution-state/command_receipts").glob("*.json"))]
                if self.output:
                    destination = Path(self.output) / self._testMethodName
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / "persisted-prefix.json").write_text(json.dumps(prefix, indent=2) + "\n")
                    for name in ("core.jsonl", "core.stderr", "terminal.json"):
                        shutil.copy2(run / name, destination / name)
            diagnostics = {"status": terminal["status"], "blocker": terminal.get("first_typed_blocker"),
                           "errors": errors, "stderr": (run / "core.stderr").read_text()[-2500:],
                           "event_tail": events[-3:]}
            self.assertTrue(terminal["cleanup_pass"], diagnostics)
            self.assertTrue(terminal["cleanup"]["engine_reaped"])
            self.assertTrue(terminal["cleanup"]["no_live_descendants"])
            verifier_cleanup = terminal["cleanup"]["parent_verifier"]
            self.assertEqual(verifier_cleanup["calls"], 1 if disconnect_after else 2)
            self.assertTrue(verifier_cleanup["cleanup_pass"])
            self.assertFalse(verifier_cleanup["channel_failure"])
            self.assertEqual(errors, [], diagnostics)
            self.assertFalse((self.f.workspace / ".tura").exists())
            self.assertEqual([row for row in _tree_rows(self.f.workspace)
                              if not row["path"].startswith("artifacts/")
                              and row["path"] not in GOLDEN],
                             [row for row in self.workspace_before if row["path"] not in GOLDEN])
            for relative, digest in GOLDEN.items():
                self.assertEqual(handoff.file_sha256(self.f.workspace / relative), digest)
            edits = [i for i, event in enumerate(events) if event.get("type") == "item.completed"
                     and event.get("item", {}).get("type") == "file_change"]
            attempts = json.loads((self.scratch / "verification_attempts.json").read_text())
            self.assertEqual(attempts[0], FAILED_BEFORE_START)
            done = [i for i, output in self.h._outputs(events)
                    if output.get("task_status", {}).get("status") == "done"]
            outcome = self._oracle()
            if disconnect_after:
                # A failed stream need not project item.completed events. Physical
                # postimages and persisted error/receipts are the effect evidence.
                failures = [row for row in prefix["runtime_events"]
                            if row.get("event") == "runtime_failed"]
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["error"]["error_code"], "CALL_FAILED_AFTER_COMMAND_EFFECT")
                self.assertFalse(failures[0]["error"]["retry_allowed"])
                self.assertFalse(failures[0]["error"]["fallback_allowed"])
                self.assertIsNone(failures[0]["usage"])
                receipts = [row for row in prefix["command_receipts"]
                            if row.get("termination_origin") == "parent_verifier"]
                self.assertEqual(len(receipts), 1, prefix["command_receipts"])
                self.assertEqual(receipts[0]["exit_code"], 75)
                self.assertEqual(receipts[0]["outcome"], "known")
                for key in ("process_reaped", "process_group_empty", "termination_proven"):
                    self.assertIs(receipts[0][key], True)
                self.assertEqual(len(calls), 1, diagnostics)
                self.assertNotEqual(terminal["status"], "RESULT_AVAILABLE", diagnostics)
                self.assertFalse(terminal.get("terminal_evidence"))
                self.assertEqual(attempts, [FAILED_BEFORE_START])
                self.assertEqual(done, [])
                self.assertFalse(outcome["verified_completion"])
            else:
                self.assertEqual(len(edits), 3, diagnostics)
                failed_index = self.h._verifier_receipt(request, events, 75, expected_calls=2)
                self.assertLess(max(edits), failed_index)
                self.assertEqual(len(calls), 3 if disconnect_before else 2, diagnostics)
                self.assertEqual(terminal["status"], "RESULT_AVAILABLE", diagnostics)
                self.assertEqual(inspected["status"], "EVIDENCE_VERIFIED", inspected)
                resolution = inspected["verifier_failure_resolution"]
                self.assertEqual(resolution["resolved_count"], 1)
                self.assertEqual(resolution["unresolved_count"], 0)
                self.assertEqual(resolution["effective_command_success"], "known_success")
                passed_index = self.h._verifier_receipt(
                    request, events, 0, ordinal=1, expected_calls=2)
                self.assertEqual(len(done), 1)
                self.assertLess(failed_index, passed_index)
                self.assertLess(passed_index, done[0])
                self.assertEqual(terminal["observed_terminal_delivery"], "evidence_only")
                marker = terminal["terminal_evidence"]
                self.assertFalse(marker["final_summary_turn_executed"])
                self.assertTrue(marker["parent_acceptance_required"])
                db_path = self.h._assert_persisted_session(request, marker)
                with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
                    saved = [json.loads(row[0]) for row in db.execute(
                        "SELECT event_json FROM runtime_events ORDER BY rowid")]
                failures = [row for row in saved if row.get("event") == "runtime_failed"]
                self.assertEqual(len(failures), int(disconnect_before))
                for failure in failures:
                    self.assertTrue(failure["error"]["retry_allowed"])
                    self.assertIsNone(failure["usage"])
                self.assertEqual(outcome["status"], "PASS", outcome)
                self.assertTrue(outcome["verified_completion"])
                self.assertEqual(len(attempts), 2)
                self.assertEqual(sum(row["test_process_count"] for row in attempts), 1)
            count = len(calls)
            postimages = _tree_rows(self.f.workspace)
            self.assertEqual(handoff.caller.execute(request), terminal)
            self.assertEqual(len(calls), count)
            self.assertEqual(len(fences), 1, "terminal recovery must not launch another engine")
            self.assertEqual(_tree_rows(self.f.workspace), postimages)
            handoff.caller.verify_runtime_image(self.h.installed_identity,
                                               required_artifacts=handoff.core.REQUIRED)
            handoff.caller.verify_runtime_image(request.runtime_image,
                                               required_artifacts=handoff.core.REQUIRED)
            self.assertEqual(len(bodies), len(calls))
            if self.output:
                destination = Path(self.output) / self._testMethodName
                destination.mkdir(parents=True, exist_ok=True)
                for name in ("core.jsonl", "core.stderr", "terminal.json"):
                    shutil.copy2(run / name, destination / name)
                record = {"schema_version": "nokiy_cutover_loopback_acceptance_v1", "status": "PASS",
                          "provider": "scripted_loopback_not_a_model_benchmark", "paid_model_calls": 0,
                          "stage_sha256": STAGE_SHA, "runtime_image_sha256": self.h.installed_identity.sha256,
                          "fixture_adaptation": "receipt I/O relocated to external scratch only",
                          "verifier_adapter_sha256": handoff.file_sha256(self.adapter),
                          "oracle_adapter_sha256": handoff.file_sha256(self.oracle_adapter),
                          "test_source_sha256": handoff.file_sha256(Path(__file__)),
                          "helper_source_sha256": handoff.file_sha256(Path(handoff.__file__)),
                          "request_id": request.request_id, "request_sha256": request.request_sha256,
                          "provider_request_count": count, "source_postimage_count": len(GOLDEN),
                          "cli_file_change_count": len(edits),
                          "first_verifier_attempt": FAILED_BEFORE_START, "verifier_attempts": attempts,
                          "baseline": baseline, "outcome": outcome, "cleanup": terminal["cleanup"],
                          "inspection": inspected, "read_only_recovery": True,
                          "full_stack_gain": "NOT_ESTABLISHED"}
                (destination / "acceptance.json").write_text(json.dumps(record, indent=2) + "\n")

    def test_two_act_semantic_repair_finishes_without_recap(self):
        self._execute()

    def test_pre_effect_stream_failure_preserves_exact_recovery(self):
        self._execute(disconnect_before=True)

    def test_post_effect_stream_failure_never_replays_patch_or_claims_done(self):
        self._execute(disconnect_after=True)


if __name__ == "__main__":
    unittest.main()
