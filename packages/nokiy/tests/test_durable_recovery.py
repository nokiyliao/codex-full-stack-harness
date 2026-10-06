# SPDX-License-Identifier: MIT
"""Quiescent failed-run fixtures, never a provider call or workspace read."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import durable_recovery, result_inspection as inspector

THREAD = "12345678-1234-1234-1234-123456789abc"


def store(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(caller._canonical_bytes(value))


def ref(path):
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


class DurableRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.prepare_run()
        process = patch.object(inspector, "_process", return_value="absent")
        process.start()
        self.addCleanup(process.stop)

    def prepare_run(self, **contract_fields):
        self.contract = {"repo_root": str(self.root / "never-read-workspace"),
                         "write_scopes": ["a.py"], "declared_targets": ["a.py"],
                         "allowed_operations": ["read", "modify", "command"], "denied_operations": []}
        self.contract.update(contract_fields)
        payload = {"schema_version": caller.REQUEST_SCHEMA_VERSION,
                   "model": "gpt-6.1-sol", "workspace": self.contract["repo_root"],
                   "native_thread_id": THREAD, "execution_profile": "direct",
                   "reasoning_effort": "max", "service_tier": "default",
                   "max_result_bytes": caller.MAX_RESULT_BYTES,
                   "jspace_contract": {"sha256": caller._canonical_sha256(self.contract)}}
        self.digest = caller._canonical_sha256(payload)
        self.rid = "tura_embedded_" + self.digest
        self.session = "full-" + self.digest
        self.run = self.root / self.rid
        self.run.mkdir(exist_ok=True)
        self.original = {**payload, "request_id": self.rid, "request_sha256": self.digest}
        self.identity = {"session_id": self.session, "cwd": payload["workspace"],
                         "model": "codex/gpt-6.1-sol", "agent": "direct",
                         "reasoning_effort": "max", "service_tier": "default"}
        self.runtime = "runtime-one"
        self.records = []
        self.feed = []
        self.index = self.run / "execution-state/session_log/index.sqlite3"
        self.db = self.index.parent / "workspaces" / ("a" * 64) / ".tura/session_log.sqlite3"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.index_path = str(self.db)
        self.lease = 0
        self.ended = 1
        self.add_command("source_read", {"exit_code": 0, "path": "a.py", "source_sha256": "b" * 64})
        self.publish()

    def add_command(self, kind, output=None, *, exit_code=0):
        n = len(self.records) // 2
        identifier = self.runtime + f".tool.command_run:call_example:{n}"
        command = {"command": kind, "command_id": identifier, "command_index": n,
                   "command_run_id": self.runtime + ".tool.command_run", "command_type": kind,
                   "provider_tool_call_id": "call_example", "step": 1, "command_line": kind + " a.py"}
        common = {k: command[k] for k in ("command_id", "command_index", "command_run_id",
                                          "command_type", "provider_tool_call_id", "step")}
        common.update(type="streamed_command_event", session_id=self.session, runtime_id=self.runtime)
        receipt = {"schema_version": "tura_command_terminal_receipt_v1", "call_id": identifier,
                   "outcome": "known", "terminal_state": "completed" if exit_code == 0 else "failed",
                   "exit_code": exit_code, "authority_effect": "none",
                   "process_reaped": True, "process_group_empty": True, "termination_proven": True}
        path = self.run / "execution-state/command_receipts" / f"command-{n}.json"
        store(path, receipt)
        body = {**(output or {}), "terminal_receipt": receipt, "terminal_receipt_path": str(path)}
        value = {**{k: command[k] for k in common if k in command}, "id": identifier,
                 "command": command.copy(), "success": exit_code == 0, "output": body}
        self.records.extend([{**common, "status": "ready", "command": command},
                             {**common, "status": "completed", "result": value}])

    def feed_progress(self, command, *, include_metadata=False, canonical_command=False):
        fields = ("command_id", "command_run_id", "provider_tool_call_id", "command_index")
        if include_metadata:
            fields += ("command_type", "step")
        return {**{key: command[key] for key in fields},
                "command": deepcopy(command) if canonical_command else command["command"], "success": None}

    def feed_update(self, command, status, result=None):
        fields = ("command_id", "command_run_id", "provider_tool_call_id", "command_index")
        return {**{key: command[key] for key in fields}, "status": status,
                "command": deepcopy(command), "result": deepcopy(result)}

    def feed_event(self, commands, *, status="running", results=(), updates=(), serialized=False):
        output = {"streamed_command_run_result": {"results": list(results)}}
        if serialized:
            output = json.dumps({"commands": commands, "results": list(results), "command_events": {}})
        return deepcopy({"event": "tool_call_updated", "session_id": self.session,
                         "runtime_id": self.runtime, "call_id": self.runtime + ".tool.command_run",
                         "state": {"status": status, "input": {"commands": commands},
                                   "output": output},
                         "command_updates": list(updates)})

    def runtime_boundary(self):
        return {"event": "tool_call_updated", "tool_name": "runtime",
                "session_id": self.session, "runtime_id": self.runtime, "call_id": self.runtime,
                "metadata": {"runtime_id": self.runtime, "session_id": self.session},
                "runtime_status": {"runtime_id": self.runtime, "status": "completed"},
                "state": {"status": "completed", "input": {"messages": [{"role": "user", "content": "Update a.py"}],
                                                          "options": {}, "tools": [{"name": "command_run"}]},
                          "output": {"success": True}}}

    def projected_feed_event(self, commands, *, results=(), updates=()):
        event = self.feed_event(commands, status="completed", results=results,
                                updates=updates, serialized=True)
        event.update(tool_name="command_run", call_id=self.runtime + "-0123456789abcdef",
                     metadata={"kind": "mano_tool_call", "tool": "command_run",
                               "runtime_id": self.runtime, "session_id": self.session})
        return event

    def publish(self):
        for path in (self.index, self.db):
            if path.exists():
                path.unlink()
        with sqlite3.connect(self.index) as db:
            db.execute("CREATE TABLE runtime_locations(runtime_id, session_id, workspace_db_path)")
            db.execute("INSERT INTO runtime_locations VALUES(?,?,?)",
                       (self.runtime, self.session, self.index_path))
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE runtimes(runtime_id, session_id, lease_active, terminal)")
            db.execute("CREATE TABLE session_context_records(session_id, sequence, record_json)")
            db.execute("INSERT INTO runtimes VALUES(?,?,?,?)", (self.runtime, self.session, self.lease, self.ended))
            db.executemany("INSERT INTO session_context_records VALUES(?,?,?)",
                           [(self.session, n, json.dumps(v)) for n, v in enumerate(self.records)])
            if self.feed:
                db.execute("CREATE TABLE session_feed_events(session_id, cursor, runtime_id, event_id, event_json)")
                db.executemany("INSERT INTO session_feed_events VALUES(?,?,?,?,?)",
                               [(self.session, n + 1, self.runtime, f"feed-{n}", json.dumps(event))
                                for n, event in enumerate(self.feed)])
        store(self.run / "request-identity.json", self.original)
        for name in ("jspace-original.json", "jspace.json"):
            store(self.run / name, self.contract)
        events = [{**self.identity, "type": "turn.started"},
                  {**self.identity, "type": "turn.completed", "status": "failed"}]
        (self.run / "core.jsonl").write_bytes(b"".join(caller._canonical_bytes(e) + b"\n" for e in events))
        scope = {"engine_pid": 456, "supervisor_pid": 789, "engine_reaped": True,
                 "no_live_descendants": True, "cleanup_error": None}
        store(self.run / "supervision.json", {"scope": scope})
        self.terminal = {"schema_version": caller.TERMINAL_SCHEMA_VERSION,
                         "execution_model": "single_task_full_core", "execution_profile": "direct",
                         "model": "gpt-6.1-sol", "reasoning_effort": "max",
                         "requested_service_tier": "default",
                         "request_id": self.rid, "request_sha256": self.digest, "native_thread_id": THREAD,
                         "status": "BLOCKED", "first_typed_blocker": "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED",
                         "cleanup_pass": True, "cleanup": scope,
                         "trajectory_artifact": ref(self.run / "core.jsonl"),
                         "supervision_artifact": ref(self.run / "supervision.json"),
                         "original_jspace_artifact": ref(self.run / "jspace-original.json"),
                         "command_evidence_artifact": None, "usage": None}
        store(self.run / "terminal.json", self.terminal)

    def inspect(self, **kwargs):
        return inspector.inspect(self.root, self.rid, THREAD, **kwargs)

    def test_feed_real_progress_and_noncompleted_updates_remain_pending(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2)]
        progress = [self.feed_progress(command, include_metadata=(n == 0), canonical_command=(n == 1))
                    for n, command in enumerate(commands)]
        self.feed = [
            self.feed_event(commands, status="ready", results=progress,
                            updates=[self.feed_update(command, "ready") for command in commands]),
            self.feed_event(commands, results=progress,
                            updates=[self.feed_update(command, "running", value)
                                     for command, value in zip(commands, progress)]),
            self.feed_event(commands, status="error", results=progress,
                            updates=[self.feed_update(commands[0], "error", progress[0]),
                                     self.feed_update(commands[1], "error")])]
        self.records = []
        self.publish()
        result = self.inspect()
        self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        self.assertEqual(result["commands"], [])
        self.assertEqual(result["command_success"], "unproven")
        details = result["durable_recovery"]
        self.assertEqual(details["pending_command_count"], 2)
        self.assertFalse(details["command_index_complete"])
        self.assertEqual(details["feed_coverage"]["event_count"], 3)
        self.assertEqual(details["file_changes"], [])
        self.assertEqual(details["historical_postimages"], [])

    def test_feed_sparse_completed_updates_alone_remain_pending(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2)]
        sparse = self.feed_event(commands, status="completed",
                                 updates=[self.feed_update(command, "completed") for command in commands])
        self.feed = [sparse, deepcopy(sparse)]
        self.records = []
        self.publish()
        result = self.inspect()
        self.assertEqual(result["commands"], [])
        self.assertEqual(result["command_success"], "unproven")
        self.assertEqual(result["terminal_status"], "BLOCKED")
        self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        details = result["durable_recovery"]
        self.assertEqual(details["pending_command_count"], 2)
        self.assertFalse(details["command_index_complete"])
        self.assertEqual(details["file_changes"], [])
        self.assertEqual(details["historical_postimages"], [])
        self.assertFalse(details["replay_allowed"])

    def test_feed_completed_results_under_aggregate_error_recover_without_replay(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update", "move_path": None}]})
        self.add_command("source_read", {"exit_code": 1, "error": "SOURCE_READ_SHA256_MISMATCH",
                                         "path": "a.py", "source_sha256": "d" * 64}, exit_code=1)
        commands = [self.records[n]["command"] for n in (0, 2, 4)]
        values = [self.records[n]["result"] for n in (1, 3, 5)]
        updates = [self.feed_update(command, "completed", value)
                   for command, value in zip(commands, values)]
        updates[0].update(command_type=commands[0]["command_type"], step=commands[0]["step"])
        completed = self.feed_event(commands, status="error", results=values, updates=updates)
        sparse = self.feed_event(commands, status="completed",
                                 updates=[self.feed_update(command, "completed") for command in commands])
        self.feed = [self.feed_event(commands, results=[self.feed_progress(command) for command in commands],
                                    updates=[self.feed_update(commands[1], "running", self.feed_progress(commands[1]))]),
                     completed, deepcopy(completed), sparse, deepcopy(sparse)]
        self.records = []
        self.publish()
        before = {path: path.read_bytes() for path in (self.run / "terminal.json", self.index, self.db)}
        with patch.object(caller, "execute", side_effect=AssertionError("replay")), \
                patch("codex_collaboration_harness.file_change_evidence._postimage", side_effect=AssertionError("workspace")):
            result = self.inspect()
        self.assertEqual(len(result["commands"]), 3)
        self.assertEqual([row["execution_proof"] for row in result["commands"]],
                         ["known_success", "known_success", "known_failure"])
        for row in result["commands"]:
            self.assertEqual(row["evidence_source"], "durable_session_feed")
            self.assertEqual(row["receipt_check"], "matched")
            self.assertEqual(row["event_index"], 2)
            self.assertEqual(row["event_sha256"], caller._canonical_sha256(completed))
        details = result["durable_recovery"]
        self.assertTrue(details["command_index_complete"])
        self.assertEqual(details["pending_command_count"], 0)
        self.assertEqual(len(details["file_changes"]), 1)
        self.assertEqual(details["file_changes"][0]["command_id"], commands[1]["command_id"])
        self.assertEqual(details["file_changes"][0]["execution_proof"], "known_success")
        self.assertEqual(details["historical_postimages"], [])
        self.assertEqual(details["current_postimage_proof"], "not_read")
        self.assertFalse(details["replay_allowed"])
        self.assertEqual(result["command_success"], "known_failure")
        self.assertEqual(result["terminal_status"], "BLOCKED")
        self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        self.assertEqual(result["file_change_success"], "unproven")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_feed_serialized_full_patch_results_recover_and_deduplicate_receipts(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2)]
        values = [self.records[n]["result"] for n in (1, 3)]
        full = self.feed_event(commands, status="error", results=values)
        snapshot = self.feed_event(commands, status="completed", results=values, serialized=True,
                                   updates=[self.feed_update(command, "completed") for command in commands])
        self.records = []
        for prior_full in (False, True):
            with self.subTest(prior_full=prior_full):
                self.feed = ([full] if prior_full else []) + [snapshot, deepcopy(snapshot)]
                self.publish()
                before = {path: path.read_bytes() for path in (self.run / "terminal.json", self.index, self.db)}
                with patch.object(caller, "execute", side_effect=AssertionError("replay")), \
                        patch("codex_collaboration_harness.file_change_evidence._postimage", side_effect=AssertionError("workspace")):
                    result = self.inspect()
                self.assertEqual(len(result["commands"]), 2)
                for row in result["commands"]:
                    self.assertEqual(row["receipt_check"], "matched")
                    self.assertEqual(row["execution_proof"], "known_success")
                    self.assertEqual(row["evidence_source"], "durable_session_feed")
                    self.assertEqual(row["event_index"], 1)
                    self.assertEqual(row["event_sha256"], caller._canonical_sha256(self.feed[0]))
                details = result["durable_recovery"]
                self.assertTrue(details["command_index_complete"])
                self.assertEqual(details["pending_command_count"], 0)
                self.assertEqual(len(details["file_changes"]), 1)
                self.assertEqual(details["file_changes"][0]["command_id"], commands[1]["command_id"])
                self.assertEqual(details["file_changes"][0]["execution_proof"], "known_success")
                self.assertEqual(details["historical_postimages"], [])
                self.assertEqual(details["current_postimage_proof"], "not_read")
                self.assertFalse(details["replay_allowed"])
                self.assertEqual(result["terminal_status"], "BLOCKED")
                self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
                self.assertEqual(result["file_change_success"], "unproven")
                self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_feed_serialized_aggregate_command_events_do_not_prove_completion(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2)]
        values = [self.records[n]["result"] for n in (1, 3)]
        event = self.feed_event(commands, status="completed")
        event["state"]["output"] = json.dumps({
            "commands": commands, "results": [],
            "command_events": {command["command_id"]: self.feed_update(command, "completed", value)
                               for command, value in zip(commands, values)},
            "task_attribution": {"task_group": "durable recovery", "task_type": ["debug"]}})
        self.feed = [event]
        self.records = []
        self.publish()
        result = self.inspect()
        self.assertEqual(result["commands"], [])
        self.assertEqual(result["command_success"], "unproven")
        self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        details = result["durable_recovery"]
        self.assertEqual(details["pending_command_count"], 2)
        self.assertFalse(details["command_index_complete"])
        self.assertEqual(details["file_changes"], [])

    def test_feed_serialized_snapshot_identity_results_and_receipt_checks_fail_closed(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2)]
        values = [self.records[n]["result"] for n in (1, 3)]
        snapshot = {"commands": commands, "results": values, "command_events": {}}
        identity = "DURABLE_COMMAND_IDENTITY_MISMATCH"
        output = "DURABLE_COMMAND_OUTPUT_INVALID"
        results = "DURABLE_FEED_RESULTS_INVALID"
        cases = [("commands missing", {key: val for key, val in snapshot.items() if key != "commands"}, identity),
                 ("commands type", {**snapshot, "commands": None}, identity),
                 ("commands order", {**snapshot, "commands": list(reversed(commands))}, identity),
                 ("command body", {**snapshot, "commands": [commands[0], {**commands[1], "command_line": "different"}]}, identity),
                 ("command index type", {**snapshot, "commands": [{**commands[0], "command_index": False}, commands[1]]}, identity),
                 ("events missing", {key: val for key, val in snapshot.items() if key != "command_events"}, output),
                 ("events type", {**snapshot, "command_events": []}, output),
                 ("results missing", {key: val for key, val in snapshot.items() if key != "results"}, results),
                 ("results type", {**snapshot, "results": None}, results),
                 ("results bound", {**snapshot, "results": [*values, values[0]]}, results),
                 ("result identity", {**snapshot, "results": [{**values[0], "command_index": False}]}, identity),
                 ("result command", {**snapshot, "results": [{**values[0], "command": {**commands[0], "command_line": "different"}}]}, identity),
                 ("result conflict", {**snapshot, "results": [values[0], {**values[0], "output": {**values[0]["output"], "stdout": "different"}}]}, identity),
                 ("missing receipt", {**snapshot, "results": [{**values[0], "output": {}}]}, "DURABLE_RECEIPT_IDENTITY_MISMATCH"),
                 ("nonboolean success", {**snapshot, "results": [{**values[0], "success": 1}]}, "DURABLE_COMMAND_STATE_UNSUPPORTED"),
                 ("patch scope", {**snapshot, "results": [{**values[1], "output": {
                     **values[1]["output"], "changes": [{"path": "other.py", "kind": "update"}]}}]}, "DURABLE_FILE_CHANGE_SCOPE_INVALID")]
        self.records = []
        for name, candidate, blocker in cases:
            for projected in (False, True):
                with self.subTest(name=name, projected=projected):
                    event = (self.projected_feed_event(commands) if projected
                             else self.feed_event(commands, status="completed"))
                    event["state"]["output"] = json.dumps(candidate)
                    self.feed = [event]
                    self.publish()
                    result = self.inspect()
                    self.assertEqual(result["first_blocker"], blocker)
                    self.assertEqual(result["commands"], [])

    def test_feed_serialized_malformed_and_duplicate_key_json_use_strict_reader(self):
        command = self.records[0]["command"]
        valid = json.dumps({"commands": [command], "results": [], "command_events": {}})
        outputs = ("invalid", valid[:-1], valid[:-1] + ', "results": []}',
                   '{"commands": [' + json.dumps(command)[:-1] + ', "step": 1}], "results": [], "command_events": {}}')
        for output in outputs:
            for projected in (False, True):
                with self.subTest(output=output, projected=projected):
                    event = (self.projected_feed_event([command]) if projected
                             else self.feed_event([command]))
                    event["state"]["output"] = output
                    with patch.object(inspector, "_json", wraps=inspector._json) as decode:
                        with self.assertRaises(inspector.InspectionError):
                            list(durable_recovery._feed_commands([(1, self.runtime, event)], self.session, inspector))
                        decode.assert_called_once_with(output.encode("utf-8"))

    def test_feed_mixed_roles_recover_deduplicated_patch_receipts_but_leave_pending_effects(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2, 4)]
        values = [self.records[n]["result"] for n in (1, 3)]
        streamed = self.feed_event(
            commands, results=[values[0], self.feed_progress(commands[1]), self.feed_progress(commands[2])])
        boundary = self.runtime_boundary()
        projected = self.projected_feed_event(commands, results=[*values, self.feed_progress(commands[2])],
                                              updates=[self.feed_update(command, "completed")
                                                       for command in commands[:2]])
        snapshot = json.loads(projected["state"]["output"])
        snapshot["command_events"] = {command["command_id"]: self.feed_update(command, "completed")
                                      for command in commands}
        projected["state"]["output"] = json.dumps(snapshot)
        projected["metadata"]["success"] = True
        self.records = []
        self.feed = [streamed, boundary, projected, deepcopy(projected)]
        self.publish()
        before = {path: path.read_bytes() for path in (self.run / "terminal.json", self.index, self.db)}
        with patch.object(caller, "execute", side_effect=AssertionError("replay")), \
                patch("codex_collaboration_harness.file_change_evidence._postimage", side_effect=AssertionError("workspace")):
            result = self.inspect()
        self.assertEqual(len(result["commands"]), 2)
        self.assertEqual({row["event_index"] for row in result["commands"]},
                         {1, 3})
        for row in result["commands"]:
            cursor = row["event_index"]
            self.assertEqual(row["receipt_check"], "matched")
            self.assertEqual(row["execution_proof"], "known_success")
            self.assertEqual(row["evidence_source"], "durable_session_feed")
            self.assertEqual(row["event_sha256"], caller._canonical_sha256(self.feed[cursor - 1]))
        details = result["durable_recovery"]
        self.assertFalse(details["command_index_complete"])
        self.assertEqual(details["pending_command_count"], 1)
        self.assertEqual(len(details["file_changes"]), 1)
        self.assertEqual(details["file_changes"][0]["command_id"], commands[1]["command_id"])
        self.assertEqual(details["file_changes"][0]["execution_proof"], "known_success")
        self.assertEqual(details["historical_postimages"], [])
        self.assertEqual(details["current_postimage_proof"], "not_read")
        self.assertFalse(details["replay_allowed"])
        self.assertEqual(result["terminal_status"], "BLOCKED")
        self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        self.assertEqual(result["file_change_success"], "unproven")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_feed_projected_completions_require_bound_metadata_and_outer_ids(self):
        command = self.records[0]["command"]
        event = self.projected_feed_event([command], results=[self.records[1]["result"]])
        event["metadata"]["success"] = True
        cases = [
            ("metadata missing", {key: val for key, val in event.items() if key != "metadata"}),
            ("metadata type", {**event, "metadata": []}),
            ("call missing", {key: val for key, val in event.items() if key != "call_id"}),
            ("tool missing", {key: val for key, val in event.items() if key != "tool_name"}),
            ("tool foreign", {**event, "tool_name": "apply_patch"}),
            ("state type", {**event, "state": None}),
            ("status missing", {**event, "state": {key: val for key, val in event["state"].items() if key != "status"}}),
            ("status running", {**event, "state": {**event["state"], "status": "running"}}),
            ("output type", {**event, "state": {**event["state"], "output": json.loads(event["state"]["output"])}}),
            ("runtime foreign", {**event, "runtime_id": "foreign-runtime"}),
            ("session foreign", {**event, "session_id": "foreign-session"})]
        for key, foreign in (("kind", "other"), ("tool", "apply_patch"),
                             ("runtime_id", "foreign-runtime"), ("session_id", "foreign-session")):
            cases.extend([
                (key + " missing", {**event, "metadata": {k: v for k, v in event["metadata"].items() if k != key}}),
                (key + " foreign", {**event, "metadata": {**event["metadata"], key: foreign}})])
        for outer in (None, 17, self.runtime, "foreign-runtime-0123456789abcdef",
                      self.runtime + "-0123456789abcde", self.runtime + "-0123456789abcdef0",
                      self.runtime + "-0123456789abcdeg", self.runtime + "-0123456789abcdeF",
                      self.runtime + ".other.command_run"):
            cases.append((f"outer id {outer!r}", {**event, "call_id": outer}))
        self.records = []
        for name, candidate in cases:
            with self.subTest(name=name):
                self.feed = [candidate]
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], "DURABLE_FEED_IDENTITY_MISMATCH")
                self.assertEqual(result["commands"], [])

    def test_feed_runtime_boundaries_reject_identity_conflicts_and_command_masquerades(self):
        command = self.records[0]["command"]
        event = self.runtime_boundary()
        cases = [
            ("call missing", {key: val for key, val in event.items() if key != "call_id"}),
            ("call foreign", {**event, "call_id": "foreign-runtime"}),
            ("command call", {**event, "call_id": command["command_run_id"]}),
            ("metadata missing", {key: val for key, val in event.items() if key != "metadata"}),
            ("metadata type", {**event, "metadata": []}),
            ("runtime status missing", {key: val for key, val in event.items() if key != "runtime_status"}),
            ("runtime status type", {**event, "runtime_status": []}),
            ("state missing", {key: val for key, val in event.items() if key != "state"}),
            ("state type", {**event, "state": []}),
            ("status missing", {**event, "state": {key: val for key, val in event["state"].items() if key != "status"}}),
            ("status running", {**event, "state": {**event["state"], "status": "running"}}),
            ("input missing", {**event, "state": {key: val for key, val in event["state"].items() if key != "input"}}),
            ("input type", {**event, "state": {**event["state"], "input": []}}),
            ("runtime foreign", {**event, "runtime_id": "foreign-runtime"}),
            ("session foreign", {**event, "session_id": "foreign-session"}),
            ("runtime status session foreign", {**event, "runtime_status": {**event["runtime_status"], "session_id": "foreign-session"}})]
        for container, key, foreign in (("metadata", "runtime_id", "foreign-runtime"),
                                        ("metadata", "session_id", "foreign-session"),
                                        ("runtime_status", "runtime_id", "foreign-runtime")):
            cases.extend([
                (container + " " + key + " missing", {**event, container: {k: v for k, v in event[container].items() if k != key}}),
                (container + " " + key + " foreign", {**event, container: {**event[container], key: foreign}})])
        for key in ("messages", "options", "tools"):
            cases.append((key + " missing", {**event, "state": {**event["state"], "input": {
                k: v for k, v in event["state"]["input"].items() if k != key}}}))
        for commands in ([], [command]):
            cases.append(("command input", {**event, "state": {**event["state"], "input": {
                **event["state"]["input"], "commands": commands}}}))
        masquerade = self.projected_feed_event([command], results=[self.records[1]["result"]])
        masquerade.update(tool_name="runtime", call_id=self.runtime,
                          runtime_status=event["runtime_status"])
        cases.append(("runtime-labelled command", masquerade))
        self.records = []
        for name, candidate in cases:
            with self.subTest(name=name):
                self.feed = [candidate]
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], "DURABLE_FEED_IDENTITY_MISMATCH")
                self.assertEqual(result["commands"], [])

    def test_feed_context_duplicates_preserve_single_effect_and_historical_postimage(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        self.add_command("source_read", {"path": "a.py", "source_sha256": "c" * 64})
        commands = [self.records[n]["command"] for n in (0, 2, 4)]
        values = [self.records[n]["result"] for n in (1, 3, 5)]
        event = self.feed_event(commands, status="error", results=values,
                                updates=[self.feed_update(command, "completed", value)
                                         for command, value in zip(commands, values)])
        self.feed = [event, deepcopy(event)]
        self.publish()
        result = self.inspect()
        self.assertEqual(len(result["commands"]), 3)
        self.assertTrue(all(row["evidence_source"] == "durable_session_context" for row in result["commands"]))
        details = result["durable_recovery"]
        self.assertTrue(details["command_index_complete"])
        self.assertEqual(len(details["file_changes"]), 1)
        self.assertEqual(len(details["historical_postimages"]), 1)
        self.assertEqual(details["historical_postimages"][0]["sha256"], "c" * 64)

    def test_feed_completed_patch_does_not_complete_pending_effect(self):
        for _ in range(2):
            self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2, 4)]
        values = [self.records[n]["result"] for n in (1, 3)]
        progress = self.feed_progress(commands[2], include_metadata=True, canonical_command=True)
        self.records = []
        for serialized in (False, True):
            with self.subTest(serialized=serialized):
                self.feed = [self.feed_event(commands, status="error", results=[*values, progress], serialized=serialized,
                                            updates=[self.feed_update(commands[0], "completed", values[0]),
                                                     self.feed_update(commands[1], "completed", values[1]),
                                                     self.feed_update(commands[2], "error", progress)])]
                self.publish()
                result = self.inspect()
                self.assertEqual(len(result["commands"]), 2)
                self.assertNotIn(commands[2]["command_id"], [row["command_id"] for row in result["commands"]])
                details = result["durable_recovery"]
                self.assertEqual(details["pending_command_count"], 1)
                self.assertFalse(details["command_index_complete"])
                self.assertEqual([change["command_id"] for change in details["file_changes"]], [commands[1]["command_id"]])
                self.assertEqual(details["historical_postimages"], [])

    def test_feed_update_stable_identity_inner_command_and_result_lineage_conflicts(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2)]
        value = self.records[3]["result"]
        update = self.feed_update(commands[1], "completed", value)
        fields = ("command_id", "command_run_id", "provider_tool_call_id", "command_index")
        cases = [("missing " + field, {key: val for key, val in update.items() if key != field}) for field in fields]
        cases += [(field, {**update, field: wrong}) for field, wrong in (
            ("command_id", commands[0]["command_id"]), ("command_run_id", "other.tool.command_run"),
            ("provider_tool_call_id", "call_other"), ("command_index", True),
            ("command_type", "source_read"), ("step", True), ("command_line", "different"),
            ("session_id", "other"), ("runtime_id", "other"), ("id", commands[0]["command_id"]),
            ("command", "apply_patch"), ("command", {**commands[1], "command_line": "different"}),
            ("result", self.records[1]["result"]))]
        self.records = []
        for name, candidate in cases:
            with self.subTest(name=name):
                self.feed = [self.feed_event(commands, status="error", updates=[candidate])]
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], "DURABLE_COMMAND_IDENTITY_MISMATCH")
                self.assertEqual(result["commands"], [])

    def test_feed_progress_identity_and_body_conflicts_fail_closed(self):
        command = self.records[0]["command"]
        progress = self.feed_progress(command)
        identity = "DURABLE_COMMAND_IDENTITY_MISMATCH"
        state = "DURABLE_COMMAND_STATE_UNSUPPORTED"
        fields = ("command_id", "command_run_id", "provider_tool_call_id", "command_index")
        cases = [("missing " + field, {key: val for key, val in progress.items() if key != field}, identity)
                 for field in fields]
        cases += [(field, {**progress, field: wrong}, identity) for field, wrong in (
            ("command_run_id", "other.tool.command_run"), ("provider_tool_call_id", "call_other"),
            ("command_index", False), ("command_type", "apply_patch"), ("step", True),
            ("command", "apply_patch"), ("command", {**command, "command_line": "different"}),
            ("command", {**command, "step": True}), ("id", "other"))]
        cases += [("null result", None, identity),
                  ("missing success", {key: val for key, val in progress.items() if key != "success"}, state),
                  ("missing command", {key: val for key, val in progress.items() if key != "command"}, state),
                  ("nonboolean success", {**progress, "success": 0}, state),
                  ("progress id", {**progress, "id": command["command_id"]}, state)]
        self.records = []
        for name, value, blocker in cases:
            with self.subTest(name=name):
                self.feed = [self.feed_event([command], results=[value])]
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], blocker)
                self.assertEqual(result["commands"], [])

    def test_feed_completed_updates_require_full_result_and_original_receipt(self):
        command = self.records[0]["command"]
        value = self.records[1]["result"]
        output = value["output"]
        identity = "DURABLE_COMMAND_IDENTITY_MISMATCH"
        state = "DURABLE_COMMAND_STATE_UNSUPPORTED"
        cases = [("list", [], identity),
                 ("progress", self.feed_progress(command), state),
                 ("missing success", {key: val for key, val in value.items() if key != "success"}, state),
                 ("null success", {**value, "success": None}, state),
                 ("integer success", {**value, "success": 1}, state),
                 ("missing id", {key: val for key, val in value.items() if key != "id"}, identity),
                 ("missing command", {key: val for key, val in value.items() if key != "command"}, identity),
                 ("string command", {**value, "command": "source_read"}, identity),
                 ("command drift", {**value, "command": {**command, "command_line": "different"}}, identity),
                 ("optional metadata conflict", {**value, "step": True}, identity),
                 ("missing output", {key: val for key, val in value.items() if key != "output"}, "DURABLE_COMMAND_OUTPUT_INVALID"),
                 ("null output", {**value, "output": None}, "DURABLE_COMMAND_OUTPUT_INVALID"),
                 ("missing receipt", {**value, "output": {}}, "DURABLE_RECEIPT_IDENTITY_MISMATCH"),
                 ("receipt identity", {**value, "output": {**output, "terminal_receipt": {
                     **output["terminal_receipt"], "call_id": "other"}}}, "DURABLE_RECEIPT_IDENTITY_MISMATCH"),
                 ("receipt mismatch", {**value, "output": {**output, "terminal_receipt": {
                     **output["terminal_receipt"], "outcome": "unknown"}}}, "RECEIPT_MISMATCH"),
                 ("receipt path missing", {**value, "output": {**output, "terminal_receipt_path": None}}, "RECEIPT_INCOMPLETE"),
                 ("receipt path escape", {**value, "output": {**output, "terminal_receipt_path": "/private/receipt.json"}}, "RECEIPT_PATH_INVALID"),
                 ("exit conflict", {**value, "output": {**output, "exit_code": 1}}, "EXIT_CODE_CONFLICT")]
        self.records = []
        for name, candidate, blocker in cases:
            with self.subTest(name=name):
                self.feed = [self.feed_event([command], status="error",
                                            updates=[self.feed_update(command, "completed", candidate)])]
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], blocker)
                self.assertEqual(result["commands"], [])
        for status in ("ready", "running", "error", "unknown"):
            with self.subTest(status=status):
                self.feed = [self.feed_event([command], updates=[self.feed_update(command, status, value)])]
                self.publish()
                self.assertEqual(self.inspect()["first_blocker"], state)

    def test_feed_completed_patch_keeps_original_scope_checks(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2)]
        value = self.records[3]["result"]
        self.records = []
        cases = [([{"path": "other.py", "kind": "update"}], "DURABLE_FILE_CHANGE_SCOPE_INVALID"),
                 ([{"path": "../a.py", "kind": "update"}], "DURABLE_FILE_CHANGE_SCOPE_INVALID"),
                 ([{"path": "a.py", "kind": "update", "move_path": "b.py"}], "DURABLE_FILE_CHANGE_SCOPE_INVALID"),
                 ([{"path": "a.py", "kind": "delete"}], "DURABLE_FILE_CHANGE_SCOPE_INVALID"),
                 ([], "DURABLE_FILE_CHANGE_INVALID")]
        for changes, blocker in cases:
            with self.subTest(changes=changes):
                candidate = {**value, "output": {**value["output"], "changes": changes}}
                self.feed = [self.feed_event(commands, status="error", results=[candidate],
                                            updates=[self.feed_update(commands[1], "completed", candidate)])]
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], blocker)
                self.assertEqual(result["commands"], [])

    def test_feed_conflicting_duplicate_bodies_and_declarations_fail_closed(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        commands = [self.records[n]["command"] for n in (0, 2)]
        values = [self.records[n]["result"] for n in (1, 3)]
        event = self.feed_event(commands, status="error", results=values,
                                updates=[self.feed_update(command, "completed", value)
                                         for command, value in zip(commands, values)])
        wrapper_conflict = deepcopy(event)
        wrapper_conflict["command_updates"][1]["result"]["output"]["stdout"] = "different"
        snapshot_conflict = deepcopy(event)
        snapshot_conflict["state"]["output"]["streamed_command_run_result"]["results"][1]["output"]["stdout"] = "different"
        serialized_conflict = self.feed_event(commands, status="error", serialized=True,
                                              results=[values[0], {**values[1], "output": {
                                                  **values[1]["output"], "stdout": "different"}}])
        declaration_conflict = deepcopy(event)
        declaration_conflict["state"]["input"]["commands"][1]["command_line"] = "different"
        self.records = []
        cases = [([wrapper_conflict], "DURABLE_COMMAND_IDENTITY_MISMATCH"),
                 ([event, snapshot_conflict], "DURABLE_COMMAND_IDENTITY_MISMATCH"),
                 ([event, serialized_conflict], "DURABLE_COMMAND_IDENTITY_MISMATCH"),
                 ([event, declaration_conflict], "DURABLE_COMMAND_DUPLICATE")]
        for feed, blocker in cases:
            with self.subTest(blocker=blocker, events=len(feed)):
                self.feed = feed
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], blocker)
                self.assertEqual(result["commands"], [])

    def test_feed_event_lineage_and_bounded_evidence_fail_closed(self):
        command = self.records[0]["command"]
        value = self.records[1]["result"]
        event = self.feed_event([command], status="error", results=[value],
                                updates=[self.feed_update(command, "completed", value)])
        lineage = "DURABLE_FEED_IDENTITY_MISMATCH"
        cases = [({**event, field: wrong}, lineage) for field, wrong in (
            ("session_id", "other"), ("runtime_id", "other"), ("event_id", "other"),
            ("cursor", True), ("call_id", "other.tool.command_run"), ("state", None))]
        cases += [({**event, "state": {**event["state"], "input": {"commands": []}}}, "DURABLE_FEED_COMMANDS_INVALID"),
                  ({**event, "state": {**event["state"], "output": []}}, "DURABLE_COMMAND_OUTPUT_INVALID"),
                  ({**event, "state": {**event["state"], "output": {
                      "streamed_command_run_result": {"results": None}}}}, "DURABLE_FEED_RESULTS_INVALID"),
                  ({**event, "state": {**event["state"], "output": {
                      "streamed_command_run_result": {"results": [value, value]}}}}, "DURABLE_FEED_RESULTS_INVALID"),
                  ({**event, "command_updates": None}, "DURABLE_FEED_RESULTS_INVALID"),
                  ({**event, "command_updates": event["command_updates"] * 3}, "DURABLE_FEED_RESULTS_INVALID")]
        self.records = []
        for candidate, blocker in cases:
            with self.subTest(blocker=blocker, event=candidate):
                self.feed = [candidate]
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], blocker)
                self.assertEqual(result["commands"], [])
        self.feed = [event]
        self.publish()
        for limit in ("MAX_FEED_ROWS", "MAX_FEED_BYTES", "MAX_RECORD_BYTES"):
            with self.subTest(limit=limit), patch.object(durable_recovery, limit, 1 if limit != "MAX_FEED_ROWS" else 0):
                self.assertEqual(self.inspect()["first_blocker"], "DURABLE_FEED_LIMIT_EXCEEDED")

    def test_original_id_recovers_without_replay_or_terminal_mutation(self):
        before = {p: p.read_bytes() for p in (self.run / "terminal.json", self.index, self.db)}
        with patch.object(caller, "execute", side_effect=AssertionError("replay")):
            result = self.inspect()
        self.assertEqual(result["terminal_status"], "BLOCKED")
        self.assertEqual(result["status"], "INCOMPLETE_EVIDENCE")
        self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        self.assertEqual(result["commands"][0]["execution_proof"], "known_success")
        self.assertEqual(result["commands"][0]["evidence_source"], "durable_session_context")
        self.assertEqual(result["artifact_integrity"], "unproven")
        self.assertFalse(result["durable_recovery"]["replay_allowed"])
        self.assertIsNone(result["usage"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse(Path(str(self.db) + "-wal").exists())

    def test_historical_patch_and_postimage_do_not_read_current_source(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update", "move_path": None}]})
        self.add_command("source_read", {"exit_code": 0, "path": "a.py", "source_sha256": "c" * 64})
        self.publish()
        with patch("codex_collaboration_harness.file_change_evidence._postimage", side_effect=AssertionError("workspace")):
            result = self.inspect()
        details = result["durable_recovery"]
        self.assertEqual(len(details["file_changes"]), 1)
        self.assertEqual(details["historical_postimages"][0]["sha256"], "c" * 64)
        self.assertEqual(details["preimage_proof"], "unavailable")
        self.assertEqual(details["current_postimage_proof"], "not_read")
        self.assertEqual(result["file_change_success"], "unproven")

    def test_known_failed_source_read_recovers_without_success_or_terminal_mutation(self):
        self.add_command("source_read", {"exit_code": 1, "error": "SOURCE_READ_SHA256_MISMATCH",
                                         "path": "a.py", "source_sha256": "c" * 64}, exit_code=1)
        self.publish()
        before = {p: p.read_bytes() for p in (self.run / "terminal.json", self.index, self.db)}
        with patch.object(caller, "execute", side_effect=AssertionError("replay")):
            result = self.inspect()
        row = result["commands"][-1]
        self.assertEqual((row["receipt_check"], row["exit_code"], row["execution_proof"]),
                         ("matched", 1, "known_failure"))
        self.assertEqual(result["command_success"], "known_failure")
        self.assertEqual(result["status"], "INCOMPLETE_EVIDENCE")
        self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        self.assertEqual(result["terminal_status"], "BLOCKED")
        self.assertEqual(result["terminal_first_blocker"], "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED")
        self.assertEqual(result["artifact_integrity"], "unproven")
        details = result["durable_recovery"]
        self.assertEqual(details["integrity"], "current_snapshot_not_sealed_by_original_terminal")
        self.assertEqual(details["historical_postimages"], [])
        self.assertEqual(details["preimage_proof"], "unavailable")
        self.assertEqual(details["current_postimage_proof"], "not_read")
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}):
            projected = inspector.project_terminal(self.terminal, self.root, self.rid)
        self.assertEqual(projected["status"], "BLOCKED")
        self.assertEqual(projected["first_typed_blocker"], "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED")
        self.assertEqual(projected["result_inspection"]["command_success"], "known_failure")
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_invalid_receipt_proof_is_not_recovered(self):
        output = self.records[1]["result"]["output"]
        receipt = output["terminal_receipt"]
        valid = receipt.copy()
        cases = [({"terminal_state": "completed", "exit_code": 1}, None),
                 ({"terminal_state": "failed", "exit_code": 0}, None),
                 ({"terminal_state": "cancelled", "exit_code": 1}, None),
                 ({"outcome": "unknown"}, None)] + [
            ({}, key) for key in ("process_reaped", "process_group_empty", "termination_proven")]
        for fields, missing in cases:
            with self.subTest(fields=fields, missing=missing):
                receipt.clear()
                receipt.update(valid)
                receipt.update(fields)
                if missing:
                    receipt.pop(missing)
                output["exit_code"] = receipt["exit_code"]
                self.records[1]["result"]["success"] = output["exit_code"] == 0
                store(Path(output["terminal_receipt_path"]), receipt)
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], "RECEIPT_PROOF_INVALID")
                self.assertEqual(result["commands"], [])
                self.assertEqual(result["command_success"], "unproven")

    def test_bool_receipt_exit_codes_rejected_in_either_durable_copy(self):
        output = self.records[1]["result"]["output"]
        receipt = output["terminal_receipt"]
        for actual_code, event_code in ((False, False), (True, True), (0, False),
                                        (1, True), (False, 0), (True, 1)):
            with self.subTest(actual=actual_code, event=event_code):
                code = int(event_code)
                receipt.update(exit_code=event_code, terminal_state="completed" if code == 0 else "failed")
                output["exit_code"] = code
                self.records[1]["result"]["success"] = code == 0
                store(Path(output["terminal_receipt_path"]), {**receipt, "exit_code": actual_code})
                self.publish()
                self.assertEqual(self.inspect()["first_blocker"], "RECEIPT_PROOF_INVALID")

    def test_durable_failed_receipt_mismatch_and_exit_conflicts_rejected(self):
        output = self.records[1]["result"]["output"]
        receipt = output["terminal_receipt"]
        receipt.update(exit_code=1, terminal_state="failed")
        output["exit_code"] = 1
        self.records[1]["result"]["success"] = False
        store(Path(output["terminal_receipt_path"]), {**receipt, "exit_code": 2})
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "RECEIPT_MISMATCH")
        store(Path(output["terminal_receipt_path"]), receipt)
        for code in (2, True):
            with self.subTest(output_code=code):
                output["exit_code"] = code
                self.publish()
                self.assertEqual(self.inspect()["first_blocker"], "EXIT_CODE_CONFLICT")

    def test_scoped_add_and_create_then_update_remain_historical_observations(self):
        for kind in ("add", "create"):
            with self.subTest(kind=kind):
                self.prepare_run(allowed_operations=["read", "modify", "create", "command"])
                self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": kind}]})
                self.add_command("source_read", {"path": "a.py", "source_sha256": "c" * 64})
                self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
                self.publish()
                with patch("codex_collaboration_harness.file_change_evidence._postimage",
                           side_effect=AssertionError("workspace")):
                    result = self.inspect()
                details = result["durable_recovery"]
                self.assertEqual([e["kind"] for e in details["file_changes"]], [kind, "update"])
                self.assertTrue(all(e["path"] == "a.py" and e["execution_proof"] == "known_success"
                                    for e in details["file_changes"]))
                self.assertEqual(details["historical_postimages"], [])
                self.add_command("source_read", {"path": "a.py", "source_sha256": "d" * 64})
                self.publish()
                before = {p: p.read_bytes() for p in (self.run / "terminal.json", self.index, self.db)}
                with patch("codex_collaboration_harness.file_change_evidence._postimage",
                           side_effect=AssertionError("workspace")):
                    result = self.inspect()
                details = result["durable_recovery"]
                self.assertEqual(details["historical_postimages"][0]["sha256"], "d" * 64)
                self.assertEqual(details["integrity"], "current_snapshot_not_sealed_by_original_terminal")
                self.assertEqual(details["preimage_proof"], "unavailable")
                self.assertEqual(details["current_postimage_proof"], "not_read")
                self.assertEqual(result["file_change_success"], "unproven")
                self.assertEqual(result["artifact_integrity"], "unproven")
                self.assertEqual(result["status"], "INCOMPLETE_EVIDENCE")
                self.assertEqual(result["terminal_status"], "BLOCKED")
                self.assertEqual(result["terminal_first_blocker"], "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED")
                self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
                self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_add_and_create_require_create_grant_and_obey_denial(self):
        for kind in ("add", "create"):
            for allowed, denied in ((["read", "modify", "command"], []),
                                    (["read", "modify", "create", "command"], ["create"])):
                with self.subTest(kind=kind, allowed=allowed, denied=denied):
                    self.prepare_run(allowed_operations=allowed, denied_operations=denied)
                    self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": kind}]})
                    self.publish()
                    self.assertEqual(self.inspect()["first_blocker"], "DURABLE_FILE_CHANGE_SCOPE_INVALID")

    def test_create_grant_does_not_require_or_authorize_modify(self):
        for denied in ([], ["modify"]):
            with self.subTest(denied=denied):
                self.prepare_run(allowed_operations=["read", "create", "command"], denied_operations=denied)
                self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "add"}]})
                self.publish()
                result = self.inspect()
                self.assertEqual(result["durable_recovery"]["file_changes"][0]["kind"], "add")
                self.assertEqual(result["file_change_success"], "unproven")
                self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
                self.publish()
                self.assertEqual(self.inspect()["first_blocker"], "DURABLE_FILE_CHANGE_SCOPE_INVALID")

    def test_add_requires_both_exact_write_and_declared_target(self):
        for field in ("write_scopes", "declared_targets"):
            with self.subTest(missing=field):
                self.prepare_run(allowed_operations=["read", "modify", "create", "command"], **{field: []})
                self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "add"}]})
                self.publish()
                self.assertEqual(self.inspect()["first_blocker"], "DURABLE_FILE_CHANGE_SCOPE_INVALID")

    def test_malicious_paths_rejected_even_if_listed_as_exact_targets(self):
        for path in ("../a.py", "./a.py", "sub/../a.py", "sub//a.py", "a.py/", "", "/outside/a.py", "a\0.py"):
            for kind in ("add", "update"):
                with self.subTest(path=path, kind=kind):
                    self.prepare_run(write_scopes=[path], declared_targets=[path],
                                     allowed_operations=["read", "modify", "create", "command"])
                    self.add_command("apply_patch", {"changes": [{"path": path, "kind": kind}]})
                    self.publish()
                    self.assertEqual(self.inspect()["first_blocker"], "DURABLE_FILE_CHANGE_SCOPE_INVALID")

    def test_move_and_delete_stay_blocked_even_with_operation_grants(self):
        for kind, move in (("delete", None), ("move", None), ("update", "b.py"), ("add", "b.py")):
            with self.subTest(kind=kind, move=move):
                self.prepare_run(write_scopes=["a.py", "b.py"], declared_targets=["a.py", "b.py"],
                                 allowed_operations=["read", "modify", "create", "delete", "command"])
                self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": kind, "move_path": move}]})
                self.publish()
                self.assertEqual(self.inspect()["first_blocker"], "DURABLE_FILE_CHANGE_SCOPE_INVALID")

    def test_failed_add_receipt_does_not_prove_source_effect_success(self):
        self.prepare_run(allowed_operations=["read", "modify", "create", "command"])
        self.add_command("apply_patch", {"exit_code": 1, "changes": [{"path": "a.py", "kind": "add"}]}, exit_code=1)
        self.publish()
        result = self.inspect()
        self.assertEqual(result["commands"][-1]["receipt_check"], "matched")
        self.assertEqual(result["command_success"], "known_failure")
        self.assertEqual(result["durable_recovery"]["file_changes"][0]["execution_proof"], "known_failure")
        self.assertEqual(result["durable_recovery"]["historical_postimages"], [])
        self.assertEqual(result["file_change_success"], "unproven")
        self.assertEqual(result["status"], "INCOMPLETE_EVIDENCE")
        self.assertEqual(result["terminal_status"], "BLOCKED")

    def test_later_patch_invalidates_prior_postimage(self):
        for _ in range(2):
            self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
            self.add_command("source_read", {"path": "a.py", "source_sha256": "c" * 64})
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}]})
        self.publish()
        self.assertEqual(self.inspect()["durable_recovery"]["historical_postimages"], [])

    def test_partial_usage_is_not_complete_or_zero_for_unknown_calls(self):
        self.records.extend([
            {"type": "runtime_usage", "runtime_id": self.runtime, "usage": {"input_tokens": 123, "output_tokens": 7}},
            {"type": "runtime_provider_observation", "runtime_id": self.runtime,
             "provider_observation": {"model": "gpt-6.1-sol"}}])
        self.publish()
        result = self.inspect()
        self.assertIsNone(result["usage"])
        self.assertEqual(result["durable_recovery"]["reported_usage"]["coverage"], "partial")
        self.assertEqual(result["durable_recovery"]["reported_usage"]["totals"], {"input_tokens": 123, "output_tokens": 7})

    def test_pending_command_remains_unproven(self):
        self.add_command("source_read")
        self.records.pop()
        self.publish()
        details = self.inspect()["durable_recovery"]
        self.assertEqual(details["pending_command_count"], 1)
        self.assertFalse(details["command_index_complete"])

    def test_request_and_trajectory_identity_rejected(self):
        self.original["model"] = "gpt-6-astra"
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "TRAJECTORY_REQUEST_IDENTITY_MISMATCH")
        self.original["model"] = "gpt-6.1-sol"
        self.identity["model"] = "codex/gpt-6-astra"
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "NOKIY_FULL_CORE_TERMINAL_IDENTITY_MISMATCH")

    def test_legacy_missing_contract_ref_still_requires_original_request_sha(self):
        self.terminal.pop("original_jspace_artifact")
        store(self.run / "terminal.json", self.terminal)
        self.assertEqual(len(self.inspect()["commands"]), 1)
        changed = {**self.contract, "allowed_operations": ["modify", "delete"]}
        for name in ("jspace-original.json", "jspace.json"):
            store(self.run / name, changed)
        self.assertEqual(self.inspect()["first_blocker"], "DURABLE_CONTRACT_MISMATCH")

    def test_wrong_thread_and_receipt_path_escape_rejected(self):
        result = inspector.inspect(self.root, self.rid, "87654321-1234-1234-1234-123456789abc")
        self.assertEqual(result["first_blocker"], "THREAD_ID_MISMATCH")
        self.records[1]["result"]["output"]["terminal_receipt_path"] = "/private/receipt.json"
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "RECEIPT_PATH_INVALID")

    def test_database_directory_symlink_rejected(self):
        original = self.db.parent
        saved = original.with_name("saved")
        original.rename(saved)
        original.symlink_to(saved, target_is_directory=True)
        self.assertEqual(self.inspect()["first_blocker"], "DURABLE_DATABASE_UNAVAILABLE")

    def test_unproven_tool_success_and_conflicting_exit_not_accepted(self):
        self.records[1]["result"]["success"] = False
        self.publish()
        self.assertEqual(self.inspect()["commands"][0]["execution_proof"], "unavailable")
        self.assertEqual(self.inspect()["command_success"], "unproven")
        self.records[1]["result"]["output"]["exit_code"] = 5
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "EXIT_CODE_CONFLICT")

    def test_record_budget_and_database_size_fail_closed(self):
        with patch.object(durable_recovery, "MAX_RECORDS", 1):
            self.assertEqual(self.inspect()["first_blocker"], "DURABLE_CONTEXT_LIMIT_EXCEEDED")
        with patch.object(durable_recovery, "MAX_DATABASE_BYTES", 1):
            self.assertEqual(self.inspect()["first_blocker"], "DURABLE_DATABASE_UNSAFE")

    def test_active_or_nonterminal_runtime_rejected(self):
        for lease, ended in ((1, 1), (0, 0)):
            self.lease, self.ended = lease, ended
            self.publish()
            self.assertEqual(self.inspect()["first_blocker"], "DURABLE_RUNTIME_NOT_TERMINAL")

    def test_wal_and_database_symlink_rejected(self):
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"unsettled")
        self.assertEqual(self.inspect()["first_blocker"], "DURABLE_DATABASE_UNSETTLED")
        wal.unlink()
        saved = self.db.with_suffix(".saved")
        self.db.rename(saved)
        self.db.symlink_to(saved)
        self.assertEqual(self.inspect()["first_blocker"], "DURABLE_DATABASE_UNAVAILABLE")

    def test_external_locator_rejected_before_read(self):
        self.index_path = "/private/not-authorized.sqlite3"
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "DURABLE_DATABASE_PATH_INVALID")

    def test_receipt_and_duplicate_result_rejected(self):
        self.records[1]["result"]["output"]["terminal_receipt"]["call_id"] = "another-call"
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "DURABLE_RECEIPT_IDENTITY_MISMATCH")
        self.records[1]["result"]["output"]["terminal_receipt"]["call_id"] = self.records[1]["command_id"]
        self.records.append(self.records[1].copy())
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "DURABLE_COMMAND_IDENTITY_MISMATCH")

    def test_missing_receipt_does_not_turn_intent_into_execution(self):
        Path(self.records[1]["result"]["output"]["terminal_receipt_path"]).unlink()
        self.assertEqual(self.inspect()["first_blocker"], "FILE_UNAVAILABLE")
        self.assertEqual(self.inspect()["commands"], [])

    def test_effect_outside_original_contract_rejected(self):
        self.add_command("apply_patch", {"changes": [{"path": "other.py", "kind": "update"}]})
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "DURABLE_FILE_CHANGE_SCOPE_INVALID")

    def test_cleanup_guard_does_not_open_durable_database(self):
        self.terminal["cleanup_pass"] = False
        store(self.run / "terminal.json", self.terminal)
        with patch.object(durable_recovery, "recover", side_effect=AssertionError("DB read")):
            self.assertEqual(self.inspect()["first_blocker"], "CLEANUP_UNPROVEN")

    def test_pagination_selection_and_cli_projection_preserve_origin(self):
        for _ in range(19):
            self.add_command("source_read")
        self.publish()
        first = self.inspect()
        self.assertEqual(len(first["commands"]), 16)
        self.assertEqual(first["pagination"]["next_offset"], 16)
        self.assertEqual(len(self.inspect(offset=16)["commands"]), 4)
        sha = first["commands"][0]["command_sha256"]
        self.assertEqual(self.inspect(command_sha256=(sha,))["command_selection"]["matches"][sha], 20)
        with patch.dict(caller.os.environ, {"CODEX_THREAD_ID": THREAD}):
            result = inspector.project_terminal(self.terminal, self.root, self.rid)
        self.assertEqual(result["result_inspection"]["commands"][0]["evidence_source"], "durable_session_context")
        self.assertLessEqual(len(caller._canonical_bytes(result)), inspector.MAX_PROJECTED_BYTES)


if __name__ == "__main__":
    unittest.main()
