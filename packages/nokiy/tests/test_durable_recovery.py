# SPDX-License-Identifier: MIT
"""Quiescent failed-run fixtures, never a provider call or workspace read."""
from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import durable_recovery, full_core, result_inspection as inspector

THREAD = "12345678-1234-1234-1234-123456789abc"


def store(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(caller._canonical_bytes(value))


def ref(path):
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


class DurableFeedBudgetTests(unittest.TestCase):
    def test_feed_beyond_the_former_8_mib_budget_stays_readable(self):
        # 24h of V251 worker DBs: 163 of 241 exceeded 8 MiB and four exceeded 64 MiB.
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE runtimes (session_id TEXT, runtime_id TEXT)")
        db.execute("CREATE TABLE session_feed_events (session_id TEXT, cursor INTEGER, runtime_id TEXT, "
                   "event_id TEXT, event_json TEXT)")
        db.execute("INSERT INTO runtimes VALUES ('s', 'r')")
        padding = "x" * (512 * 1024)
        for cursor in range(1, 21):
            event = {"event": "message_upserted", "session_id": "s", "runtime_id": "r",
                     "event_id": f"e{cursor}", "cursor": cursor, "text": padding}
            db.execute("INSERT INTO session_feed_events VALUES ('s', ?, 'r', ?, ?)",
                       (cursor, f"e{cursor}", json.dumps(event)))
        records, coverage = durable_recovery._feed(db, "s", {"r"}, inspector)
        self.assertEqual(len(records), 20)
        self.assertEqual(coverage["event_count"], 20)
        self.assertGreater(sum(len(json.dumps(event)) for _, _, event in records), 8 * 1024 * 1024)


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

    def add_command(self, kind, output=None, *, exit_code=0, provider="call_example"):
        n = len(self.records) // 2
        identifier = self.runtime + f".tool.command_run:{provider}:{n}"
        command = {"command": kind, "command_id": identifier, "command_index": n,
                   "command_run_id": self.runtime + ".tool.command_run", "command_type": kind,
                   "provider_tool_call_id": provider, "step": 1, "command_line": kind + " a.py"}
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

    def policy_denial(self):
        self.add_command("zsh")
        ready, completed = self.records[-2:]
        command = deepcopy(ready["command"])
        value = {**{k: command[k] for k in durable_recovery._COMMAND_FIELDS},
                 "command": command, "command_type": "jspace", "success": False,
                 "effect_state": "not_started", "mutation_count": 0, "authority_effect": "none",
                 "delivery_state": "deterministic_policy_denial", "replayable": True,
                 "jspace_error_code": "JSPACE_READ_COMMAND_DENIED", "operation": "read",
                 "target": "missing.py", "error": "JSPACE_READ_COMMAND_DENIED: missing operand"}
        completed.update(command_type="jspace", result=value)
        return command, value

    def test_bound_policy_denial_preserves_other_command_evidence(self):
        self.policy_denial()
        self.publish()
        result = self.inspect()
        self.assertEqual(result["commands"][0]["execution_proof"], "known_success")
        denied = result["commands"][-1]
        self.assertEqual(denied["status"], "policy_denied")
        self.assertEqual(denied["execution_proof"], "not_started")
        self.assertIsNone(denied["exit_code"])
        self.assertFalse(denied["automatic_retry_allowed"])

    def test_policy_denial_feed_and_context_agree(self):
        command, value = self.policy_denial()
        command["command_index"] = 0
        command["command_id"] = command["command_run_id"] + ":" + command["provider_tool_call_id"] + ":0"
        value.update(command=command, command_id=command["command_id"], command_index=0)
        records = [(1, self.runtime, self.feed_event([command], status="completed", results=[value]))]
        events = list(durable_recovery._feed_commands(records, self.session, inspector))
        self.assertEqual(events[-1][1]["result"]["command_type"], "jspace")

    def test_policy_denial_rejects_identity_and_effect_conflicts(self):
        command, value = self.policy_denial()
        for field, changed in (("mutation_count", 1), ("mutation_count", False),
                               ("effect_state", "started"), ("command_id", "other"),
                               ("success", True), ("output", {})):
            with self.subTest(field=field, changed=changed):
                bad = {**value, field: changed}
                with self.assertRaisesRegex(inspector.InspectionError, "DURABLE_COMMAND_IDENTITY_MISMATCH"):
                    durable_recovery._policy_denial(bad, command, inspector)

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

    def interrupted(self):
        self.lease, self.ended = 1, 0
        self.publish()
        (self.run / "terminal.json").unlink()
        value = json.loads((self.run / "supervision.json").read_bytes())
        value.update(failure="GRAPH_SUPERVISOR_CALLER_EXITED")
        value["scope"]["engine_exit_code"] = -15
        store(self.run / "supervision.json", value)

    def test_opaque_provider_ids_preserve_context_and_feed_bindings(self):
        for provider in ("scripted-command-0", "toolu_01opaque", "call_example"):
            for source in ("context", "feed", "both"):
                with self.subTest(provider=provider, source=source):
                    self.records, self.feed = [], []
                    self.add_command("source_read", provider=provider)
                    command, value = self.records[0]["command"], self.records[1]["result"]
                    if source != "context":
                        self.feed = [self.feed_event([command], status="error",
                            updates=[self.feed_update(command, "completed", value)])]
                    if source == "feed":
                        self.records = []
                    self.publish()
                    before = (self.run / "terminal.json").read_bytes()
                    with patch.object(caller, "execute", side_effect=AssertionError("no replay")):
                        result = self.inspect()
                    self.assertEqual(len(result["commands"]), 1)
                    self.assertEqual(result["commands"][0]["command_id"], command["command_id"])
                    self.assertEqual(result["commands"][0]["receipt_check"], "matched")
                    self.assertFalse(result["durable_recovery"]["replay_allowed"])
                    self.assertEqual(result["terminal_status"], "BLOCKED")
                    self.assertEqual(before, (self.run / "terminal.json").read_bytes())

    def test_provider_id_budget_and_structural_context_fields_fail_closed(self):
        invalid = ("", None, False, "call_\0", "call_\n", "call_\x7f", "call_\x85",
                   "call_\u2028", "call_" + "x" * 256, "call_" + "\u00e9" * 126)
        for provider in invalid:
            for source in ("context", "feed"):
                with self.subTest(provider=provider, source=source):
                    self.records, self.feed = [], []
                    self.add_command("source_read", provider=provider)
                    if source == "feed":
                        command, value = self.records[0]["command"], self.records[1]["result"]
                        self.feed = [self.feed_event([command], status="error",
                            updates=[self.feed_update(command, "completed", value)])]
                        self.records = []
                    self.publish()
                    result = self.inspect()
                    self.assertEqual(result["first_blocker"], "DURABLE_COMMAND_IDENTITY_MISMATCH")
                    self.assertEqual(result["commands"], [])
        for field, wrong in (("session_id", "foreign"), ("runtime_id", "foreign"),
                ("command_run_id", "foreign.tool.command_run"), ("provider_tool_call_id", "other-opaque"),
                ("command_id", self.runtime + ".tool.command_run:call_example:1"),
                ("command_index", True), ("command_index", -1), ("command_index", 128),
                ("command_type", ""), ("step", True)):
            with self.subTest(field=field, wrong=wrong):
                self.records, self.feed = [], []
                self.add_command("source_read")
                self.records[0][field] = wrong
                self.publish()
                result = self.inspect()
                self.assertIn(result["first_blocker"], ("DURABLE_COMMAND_IDENTITY_MISMATCH",
                                                       "DURABLE_RUNTIME_IDENTITY_MISMATCH"))
                self.assertEqual(result["commands"], [])

    def test_interrupted_cli_recovers_without_terminal_or_execution(self):
        self.interrupted()
        before = {str(p): p.read_bytes() for p in self.run.rglob('*') if p.is_file()}
        output = io.StringIO()
        with patch.dict('os.environ', CODEX_THREAD_ID=THREAD), redirect_stdout(output), \
                patch.object(caller, 'execute', side_effect=AssertionError('no execution')):
            code = caller.main(['read-result', '--artifact-root', str(self.root),
                                '--request-id', self.rid, '--summary', '--require-verified-evidence'])
        self.assertEqual(code, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'INTERRUPTED_EVIDENCE_AVAILABLE')
        self.assertFalse(result['automatic_retry_allowed'])
        self.assertFalse(result['task_success'])
        self.assertFalse(result['durable_recovery']['command_index_complete'])
        self.assertFalse(result['durable_recovery']['runtime_rows_terminal'])
        self.assertEqual(result['commands'][0]['receipt_check'], 'matched')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.run.rglob('*') if p.is_file()})

    def test_interrupted_pending_command_remains_unknown(self):
        self.records.pop()
        self.interrupted()
        result = durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)
        self.assertEqual(result['commands'], [])
        self.assertEqual(result['durable_recovery']['pending_command_count'], 1)
        self.assertEqual(result['durable_recovery']['unobserved_filesystem_effects'], 'UNKNOWN')

    def test_interrupted_receipted_edit_not_hidden_or_accepted(self):
        self.add_command('apply_patch', {'changes': [{'path': 'a.py', 'kind': 'update'}]})
        self.interrupted()
        result = durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)
        self.assertEqual(len(result['durable_recovery']['file_changes']), 1)
        self.assertEqual(result['durable_recovery']['current_postimage_proof'], 'not_read')
        self.assertFalse(result['task_success'])

    def test_interrupted_foreign_thread_and_changed_identity_rejected(self):
        self.interrupted()
        for thread in ('foreign', '12345678-1234-1234-1234-123456789abd', None):
            with self.subTest(thread=thread), self.assertRaises(inspector.InspectionError):
                durable_recovery.inspect_interrupted(self.root, self.rid, thread)
        original = deepcopy(self.original)
        original['model'] = 'foreign-model'
        store(self.run / 'request-identity.json', original)
        with self.assertRaisesRegex(inspector.InspectionError, 'IDENTITY_MISMATCH'):
            durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)

    def test_interrupted_live_or_unknown_process_blocks_snapshot(self):
        self.interrupted()
        for state in ('present_unowned', 'permission_unknown', 'unknown'):
            with patch.object(inspector, '_process', return_value=state), \
                    self.assertRaisesRegex(inspector.InspectionError, 'CLEANUP_UNVERIFIED'):
                durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)

    def test_interrupted_incomplete_cleanup_and_wrong_failure_rejected(self):
        self.interrupted()
        path = self.run / 'supervision.json'
        original = json.loads(path.read_bytes())
        changes = [('engine_reaped', False), ('no_live_descendants', False),
                   ('cleanup_error', 'failed'), ('engine_exit_code', 0)]
        for key, value in changes:
            changed = deepcopy(original)
            changed['scope'][key] = value
            store(path, changed)
            with self.subTest(key=key), self.assertRaisesRegex(inspector.InspectionError, 'CLEANUP_UNVERIFIED'):
                durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)
        original['failure'] = 'UNRELATED_FAILURE'
        store(path, original)
        with self.assertRaisesRegex(inspector.InspectionError, 'CLEANUP_UNVERIFIED'):
            durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)

    def test_interrupted_malformed_or_linked_terminal_never_falls_back(self):
        self.interrupted()
        path = self.run / 'terminal.json'
        path.write_text('invalid')
        with self.assertRaisesRegex(inspector.InspectionError, 'ORIGINAL_TERMINAL_PRESENT'):
            durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)
        path.unlink()
        path.symlink_to(self.run / 'missing')
        with self.assertRaisesRegex(inspector.InspectionError, 'ORIGINAL_TERMINAL_PRESENT'):
            durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)

    def test_interrupted_unsettled_database_not_read(self):
        self.interrupted()
        Path(str(self.db) + '-wal').write_bytes(b'pending')
        with self.assertRaisesRegex(inspector.InspectionError, 'DATABASE_UNSETTLED'):
            durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)

    def test_interrupted_tampered_receipt_not_accepted(self):
        self.interrupted()
        path = self.run / 'execution-state/command_receipts/command-0.json'
        value = json.loads(path.read_bytes())
        value['exit_code'] = 1
        store(path, value)
        with self.assertRaises(inspector.InspectionError):
            durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)

    def test_interrupted_evidence_change_during_read_rejected(self):
        self.interrupted()
        original = durable_recovery.recover
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.run / 'core.jsonl').write_text('{}\n')
            return result
        with patch.object(durable_recovery, 'recover', side_effect=changing), \
                self.assertRaisesRegex(inspector.InspectionError, 'EVIDENCE_CHANGED'):
            durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)

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

    def test_feed_failed_updates_recover_known_failure_without_replay(self):
        # Tura publishes a receipted nonzero command as update status "failed";
        # its context copy is the same result recorded as completed/success=false.
        self.add_command("source_read", {"exit_code": 1, "error_type": "SOURCE_READ_RANGE_OUT_OF_BOUNDS"},
                         exit_code=1)
        commands = [self.records[n]["command"] for n in (0, 2)]
        values = [self.records[n]["result"] for n in (1, 3)]
        running = self.feed_event(commands, updates=[
            self.feed_update(commands[1], "ready"), self.feed_update(commands[1], "failed", values[1])])
        completed = self.feed_event(commands, status="completed", updates=[
            self.feed_update(commands[0], "completed", values[0]),
            self.feed_update(commands[1], "failed", values[1])])
        # The projected wrapper of a failed batch ends as a bare error notice.
        wrapper = self.projected_feed_event(commands)
        del wrapper["state"]["output"]
        wrapper["state"].update(status="error", error="Tool execution failed")
        wrapper["command_updates"] = []
        self.feed = [running, wrapper, completed, deepcopy(completed)]
        self.records = []
        self.publish()
        before = {path: path.read_bytes() for path in (self.run / "terminal.json", self.index, self.db)}
        with patch.object(caller, "execute", side_effect=AssertionError("replay")):
            result = self.inspect()
        self.assertEqual([(row["command_id"], row["exit_code"], row["execution_proof"], row["receipt_check"])
                          for row in result["commands"]],
                         [(commands[1]["command_id"], 1, "known_failure", "matched"),
                          (commands[0]["command_id"], 0, "known_success", "matched")])
        details = result["durable_recovery"]
        self.assertEqual(details["pending_command_count"], 0)
        self.assertTrue(details["command_index_complete"])
        self.assertFalse(details["replay_allowed"])
        self.assertEqual(result["command_success"], "known_failure")
        self.assertEqual(result["first_blocker"], "TERMINAL_NOT_AVAILABLE")
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        # "failed" never completes a successful result or a progress-only record.
        for name, command, candidate in (("success under failed", commands[0], values[0]),
                                         ("progress under failed", commands[1], self.feed_progress(commands[1]))):
            with self.subTest(name=name):
                self.feed = [self.feed_event(commands, updates=[self.feed_update(command, "failed", candidate)])]
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], "DURABLE_COMMAND_STATE_UNSUPPORTED")
                self.assertEqual(result["commands"], [])
        # An error wrapper may not carry results or updates of its own.
        with_output = deepcopy(wrapper)
        with_output["state"]["output"] = self.projected_feed_event(commands)["state"]["output"]
        with_updates = deepcopy(wrapper)
        with_updates["command_updates"] = [self.feed_update(commands[1], "failed", values[1])]
        for name, event in (("error wrapper output", with_output), ("error wrapper updates", with_updates)):
            with self.subTest(name=name):
                self.feed = [running, event]
                self.publish()
                result = self.inspect()
                self.assertEqual(result["first_blocker"], "DURABLE_FEED_IDENTITY_MISMATCH")
                self.assertEqual(result["commands"], [])

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
        for status in ("ready", "running", "error", "failed", "unknown"):
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

    def test_invalid_receipt_proof_keeps_verified_neighbor_and_precise_diagnostic(self):
        self.add_command("source_read")
        output = self.records[1]["result"]["output"]
        receipt = output["terminal_receipt"]
        valid = receipt.copy()
        cases = [("schema_version", "unsupported", "tura_command_terminal_receipt_v1"),
                 ("outcome", "unknown", "known"), ("exit_code", "0", "int"),
                 ("exit_code", None, "int"), ("terminal_state", "failed", "completed")] + [
            (key, None, True) for key in ("process_reaped", "process_group_empty", "termination_proven")]
        for field, bad, expected in cases:
            with self.subTest(field=field, bad=bad):
                receipt.clear()
                receipt.update(valid)
                if bad is None:
                    receipt.pop(field)
                else:
                    receipt[field] = bad
                store(Path(output["terminal_receipt_path"]), receipt)
                self.publish()
                before = {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
                with patch.object(caller, "execute", side_effect=AssertionError("replay")):
                    result = self.inspect()
                self.assertEqual(result["first_blocker"], "RECEIPT_PROOF_INVALID")
                self.assertEqual(result["command_success"], "unproven")
                unknown, verified = result["commands"]
                self.assertEqual((unknown["status"], unknown["receipt_check"], unknown["exit_code"], unknown["execution_proof"]),
                                 ("unknown", "invalid", None, "unavailable"))
                self.assertEqual(verified["execution_proof"], "known_success")
                detail = result["first_blocker_detail"]
                self.assertEqual(detail, unknown["first_blocker_detail"])
                self.assertEqual((detail["failed_field"], detail["expected"]), (field, expected))
                self.assertEqual(detail["failed_predicate"], "type_is_int" if field == "exit_code" else
                                 "is_true" if field.startswith("process_") or field == "termination_proven" else "equals")
                self.assertEqual(detail["receipt_copy"], "file")
                self.assertEqual(detail["request_locator"], {"request_id": self.rid, "artifact_root": str(self.root)})
                self.assertEqual(detail["receipt_path"], output["terminal_receipt_path"])
                self.assertEqual(detail["identity"], {key: self.records[1][key] for key in
                    ("command_id", "command_run_id", "runtime_id", "provider_tool_call_id", "command_index")})
                self.assertEqual(detail["event_index"], 1)
                self.assertEqual(result["observation_availability"], "partial")
                self.assertEqual(result["aggregate_execution_proof"], "unproven")
                self.assertFalse(result["durable_recovery"]["command_index_complete"])
                for flag in ("task_success", "automatic_retry_allowed", "execution_replayed"):
                    self.assertIs(result[flag], False)
                self.assertEqual(before, {p: p.read_bytes() for p in before})

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
                result = self.inspect()
                self.assertEqual(result["first_blocker"], "RECEIPT_PROOF_INVALID")
                detail = result["first_blocker_detail"]
                self.assertEqual((detail["failed_field"], detail["failed_predicate"], detail["observed"]["type"]),
                                 ("exit_code", "type_is_int", "bool"))
                self.assertEqual(detail["receipt_copy"], "file" if type(actual_code) is bool else "embedded")
                self.assertEqual(result["commands"][0]["execution_proof"], "unavailable")

    def test_mixed_interrupted_cli_preserves_observations_and_original_bytes_without_replay(self):
        self.add_command("apply_patch", {"changes": [{"path": "a.py", "kind": "update"}],
                                         "credentials": "RECEIPT_SECRET_SENTINEL"})
        invalid = self.records[3]["result"]["output"]
        invalid["terminal_receipt"]["process_reaped"] = False
        store(Path(invalid["terminal_receipt_path"]), invalid["terminal_receipt"])
        self.add_command("source_read", {"exit_code": 1}, exit_code=1)
        self.interrupted()
        before = {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        command = ["read-result", "--artifact-root", str(self.root), "--request-id", self.rid]
        with patch.object(caller, "execute", side_effect=AssertionError("replay")), \
                patch.object(caller, "_prepare", side_effect=AssertionError("fresh execution")), \
                patch.dict(caller.os.environ, CODEX_THREAD_ID=THREAD):
            for flags in ([], ["--summary", "--require-verified-evidence"]):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = caller.main(command + flags)
                self.assertEqual(code, 2)
                result = json.loads(output.getvalue())
                self.assertEqual(result["status"], "EXECUTION_UNCERTAIN")
                self.assertEqual(result["first_typed_blocker"], "RECEIPT_PROOF_INVALID")
                self.assertEqual(result["request_id"], self.rid)
                self.assertEqual([row["execution_proof"] for row in result["commands"]],
                                 ["known_success", "unavailable", "known_failure"])
                self.assertEqual(result["first_blocker_detail"]["identity"]["command_index"], 1)
                self.assertEqual(result["observation_availability"], "partial")
                self.assertEqual(result["aggregate_execution_proof"], "unproven")
                self.assertEqual(result["mission_acceptance"], "parent_owned")
                for flag in ("task_success", "automatic_retry_allowed", "execution_replayed", "terminal_synthesized"):
                    self.assertIs(result[flag], False)
                details = result["durable_recovery"]
                self.assertEqual((details["verified_command_count"], details["invalid_receipt_count"]), (2, 1))
                self.assertEqual(details["file_changes"], [])
                self.assertEqual(details["historical_postimages"], [])
                self.assertEqual(details["unobserved_filesystem_effects"], "UNKNOWN")
                self.assertNotIn("RECEIPT_SECRET_SENTINEL", output.getvalue())
        self.assertFalse((self.run / "terminal.json").exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()})

    def test_mixed_feed_receipts_keep_independent_observations(self):
        self.add_command("source_read")
        output = self.records[3]["result"]["output"]
        output["terminal_receipt"]["termination_proven"] = False
        store(Path(output["terminal_receipt_path"]), output["terminal_receipt"])
        commands = [record["command"] for record in self.records[::2]]
        values = [record["result"] for record in self.records[1::2]]
        self.feed = [self.feed_event(commands, status="completed", results=values)]
        self.records = []
        self.interrupted()
        result = durable_recovery.inspect_interrupted(self.root, self.rid, THREAD)
        self.assertEqual(result["status"], "EXECUTION_UNCERTAIN")
        self.assertEqual([row["execution_proof"] for row in result["commands"]], ["known_success", "unavailable"])
        self.assertTrue(all(row["evidence_source"] == "durable_session_feed" for row in result["commands"]))
        self.assertEqual(result["first_blocker_detail"]["identity"]["command_id"], commands[1]["command_id"])

    def test_partial_observations_do_not_bypass_global_integrity_failures(self):
        for failure in ("path", "copy", "command_identity", "receipt_identity", "exit", "aggregate", "contract", "digest", "owner", "hash"):
            with self.subTest(failure=failure):
                self.prepare_run()
                invalid = self.records[1]["result"]["output"]
                invalid["terminal_receipt"]["process_group_empty"] = False
                store(Path(invalid["terminal_receipt_path"]), invalid["terminal_receipt"])
                self.add_command("source_read", {"exit_code": 0})
                last = self.records[3]["result"]["output"]
                if failure == "path":
                    last["terminal_receipt_path"] = str(self.root / "outside.json")
                elif failure == "command_identity":
                    self.records[3]["command_index"] = 0
                elif failure == "receipt_identity":
                    last["terminal_receipt"]["call_id"] = "foreign"
                elif failure == "exit":
                    last["exit_code"] = 2
                elif failure == "aggregate":
                    invalid["results"] = []
                elif failure == "digest":
                    self.original["model"] = "foreign"
                self.publish()
                if failure == "copy":
                    store(Path(last["terminal_receipt_path"]), {**last["terminal_receipt"], "exit_code": 7})
                elif failure == "contract":
                    changed = {**self.contract, "allowed_operations": ["read"]}
                    self.terminal.pop("original_jspace_artifact")
                    store(self.run / "terminal.json", self.terminal)
                    for name in ("jspace-original.json", "jspace.json"):
                        store(self.run / name, changed)
                elif failure == "hash":
                    trajectory = self.run / "core.jsonl"
                    trajectory.write_bytes(trajectory.read_bytes()[:-1] + b" ")
                thread = "87654321-1234-1234-1234-123456789abc" if failure == "owner" else THREAD
                result = inspector.inspect(self.root, self.rid, thread)
                expected = {"path": "RECEIPT_PATH_INVALID", "copy": "RECEIPT_MISMATCH",
                            "command_identity": "DURABLE_COMMAND_IDENTITY_MISMATCH",
                            "receipt_identity": "DURABLE_RECEIPT_IDENTITY_MISMATCH", "exit": "EXIT_CODE_CONFLICT",
                            "aggregate": "RECEIPT_PROOF_INVALID", "contract": "DURABLE_CONTRACT_MISMATCH",
                            "digest": "TRAJECTORY_REQUEST_IDENTITY_MISMATCH", "owner": "THREAD_ID_MISMATCH"}
                if failure == "hash":
                    self.assertIn("HASH_MISMATCH", result["first_blocker"])
                else:
                    self.assertEqual(result["first_blocker"], expected[failure])
                self.assertEqual(result["commands"], [])
                self.assertEqual(result["artifact_integrity"], "unproven")
                self.assertNotIn("durable_recovery", result)

    def test_invalid_aggregate_cli_keeps_precise_failure_without_partial_observations(self):
        self.add_command("source_read")
        output = self.records[3]["result"]["output"]
        output["results"] = []
        output["terminal_receipt"]["termination_proven"] = False
        store(Path(output["terminal_receipt_path"]), output["terminal_receipt"])
        self.interrupted()
        before = {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        output = io.StringIO()
        with patch.dict(caller.os.environ, CODEX_THREAD_ID=THREAD), redirect_stdout(output), \
                patch.object(caller, "execute", side_effect=AssertionError("replay")):
            code = caller.main(["read-result", "--artifact-root", str(self.root), "--request-id", self.rid])
        self.assertEqual(code, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "EXECUTION_UNCERTAIN")
        self.assertEqual(result["first_typed_blocker"], "RECEIPT_PROOF_INVALID")
        self.assertEqual(result["first_blocker_detail"]["failed_field"], "termination_proven")
        self.assertEqual(result.get("commands", []), [])
        for flag in ("task_success", "automatic_retry_allowed", "execution_replayed"):
            self.assertIs(result[flag], False)
        self.assertEqual(before, {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()})

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

    def test_failed_attempt_usage_survives_recovery_without_double_counting(self):
        observation = {"type": "runtime_provider_observation", "runtime_id": self.runtime,
                       "provider_observation": {"failed_attempts": [
                           {"usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                                      "cached_input_tokens": 0, "invalid_fields": []}},
                           {"usage": None},
                           {"usage": {"input_tokens": -1, "invalid_fields": ["/input_tokens"]}},
                       ]}}
        self.records.extend([observation, deepcopy(observation),
                             {"type": "runtime_usage", "runtime_id": self.runtime,
                              "usage": {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}}])
        self.publish()
        result = self.inspect()
        details = result["durable_recovery"]
        self.assertIsNone(result["usage"])
        self.assertEqual(details["reported_usage"]["totals"]["total_tokens"], 24)
        failed = details["reported_failed_attempt_usage"]
        self.assertEqual(failed["attempt_count"], 3)
        self.assertEqual(failed["totals"]["total_tokens"], 12)
        self.assertEqual(failed["totals"]["cached_input_tokens"], 0)
        self.assertNotIn("reasoning_tokens", failed["totals"])
        self.assertEqual(failed["unknown_attempt_count"], 1)
        self.assertEqual(failed["invalid_attempt_count"], 1)
        self.assertEqual(failed["coverage"], "partial")
        self.assertEqual(failed["billing"], "UNKNOWN")
        self.assertFalse(failed["included_in_reported_usage_totals"])
        self.records[3]["provider_observation"]["failed_attempts"] = []
        self.publish()
        self.assertEqual(self.inspect()["first_blocker"], "DURABLE_FAILED_USAGE_CONFLICT")

    def test_failed_attempt_usage_invalid_or_missing_fields_never_become_zero(self):
        for value in [
            {"input_tokens": True, "invalid_fields": []},
            {"input_tokens": 2, "output_tokens": 1, "total_tokens": 2, "invalid_fields": []},
            {"input_tokens": 2, "cached_input_tokens": 3, "invalid_fields": []},
            {"output_tokens": 2, "reasoning_tokens": 3, "invalid_fields": []},
        ]:
            summary = durable_recovery._failed_usage_summary({"r": [{"usage": value}]})
            self.assertEqual(summary["totals"], {})
            self.assertEqual(summary["invalid_attempt_count"], 1)
        summary = durable_recovery._failed_usage_summary({})
        self.assertEqual(summary["coverage"], "partial")
        self.assertEqual(summary["totals"], {})

    def test_normal_terminal_preserves_failed_usage_summary_without_reclassification(self):
        usage = {"total_tokens": 24, "reported_failed_attempt_usage":
                 durable_recovery._failed_usage_summary({"r": [
                     {"usage": {"total_tokens": 12, "invalid_fields": []}}, {"usage": None}]})}
        turn = full_core._TurnSummary()
        turn.observe({"type": "turn.completed", "status": "completed", "usage": usage})
        turn.finish()
        self.assertEqual(turn.usage, usage)
        self.assertEqual(turn.usage["total_tokens"], 24)
        self.assertEqual(turn.usage["reported_failed_attempt_usage"]["totals"]["total_tokens"], 12)

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
