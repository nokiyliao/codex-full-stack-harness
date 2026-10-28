"""Request-local SQLite fixtures only; no native runtime or provider execution."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_collaboration_harness import durable_recovery, full_core as core, result_inspection as inspector


class ProviderStreamFailureSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        digest = "a" * 64
        self.session, self.runtime = "full-" + digest, "runtime-one"
        self.run = self.root / ("tura_embedded_" + digest)
        self.index = self.run / "execution-state/session_log/index.sqlite3"
        self.db = self.index.parent / "workspaces" / ("b" * 64) / ".tura/session_log.sqlite3"
        self.db.parent.mkdir(parents=True)
        self.snapshot = {"schema_version": "provider_stream_failure_snapshot_v1", "event_count": 6,
            "completed_seen": False, "completion_valid": False, "completed_frame_ended": False,
            "pending_bytes": 0, "request_id": None, "runtime_id": self.runtime,
            "command_effect_started": True}
        self.terminal = {"status": "BLOCKED", "cleanup_pass": True,
            "cleanup": {"engine_reaped": True, "no_live_descendants": True, "cleanup_error": None},
            "first_typed_blocker": "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED",
            "request_id": self.run.name, "request_sha256": digest,
            "trajectory_artifact": {"path": str(self.run / "core.jsonl"), "sha256": "c" * 64, "bytes": 1}}
        self.diagnostic = {"schema_version": "nokiy_provider_failure_diagnostic_v1", "event_index": 2,
            "trajectory_artifact": self.terminal["trajectory_artifact"], "transport": core._FAILED_TRANSPORT,
            "execution_evidence": {"schema_version": "execution_evidence_v1", "session_id": self.session,
                "status": "persisted_prefix", "from_sequence": 0, "next_sequence": 1,
                "terminal_boundary_verified": False}, "model_request_started": "UNKNOWN",
            "effect_outcome": "UNKNOWN", "parent_acceptance_required": True}
        with sqlite3.connect(self.index) as db:
            db.execute("CREATE TABLE runtime_locations(runtime_id,session_id,workspace_db_path)")
            db.execute("INSERT INTO runtime_locations VALUES(?,?,?)", (self.runtime, self.session, str(self.db)))
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE sessions(session_id,next_context_sequence,retained_from_sequence)")
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (self.session, 1, 0))
            db.execute("CREATE TABLE session_context_records(session_id,sequence,record_json)")
            db.execute("INSERT INTO session_context_records VALUES(?,?,?)", (self.session, 0,
                json.dumps({"type": "runtime_provider_observation", "runtime_id": self.runtime})))
            db.execute("CREATE TABLE runtimes(runtime_id,session_id,lease_active,terminal,last_event_seq)")
            db.execute("INSERT INTO runtimes VALUES(?,?,?,?,?)", (self.runtime, self.session, 0, 1, 3))
            db.execute("CREATE TABLE runtime_events(runtime_id,event_seq,event_json)")
            db.executemany("INSERT INTO runtime_events VALUES(?,?,?)", [
                (self.runtime, 1, json.dumps({"runtime_id": self.runtime})),
                (self.runtime, 2, json.dumps(self.output())),
                (self.runtime, 3, json.dumps(self.failure()))])

    def output(self):
        return {"runtime_id": self.runtime, "output": {"error": core._FAILED_TRANSPORT_ERROR.removeprefix(
            "manas processing failed: "), "provider_stream_failure": self.snapshot}}

    def failure(self):
        return {"runtime_id": self.runtime, "error": {"error_code": "CALL_FAILED_AFTER_COMMAND_EFFECT",
            "retry_allowed": False, "fallback_allowed": False}}

    def collect(self):
        return core.provider_failure_diagnostic_with_snapshot(self.diagnostic, self.terminal, self.run)

    def update_event(self, sequence, value):
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runtime_events SET event_json=? WHERE event_seq=?", (value, sequence))

    def assert_omitted(self):
        before = deepcopy(self.terminal)
        self.assertEqual(self.collect(), self.diagnostic)
        self.assertEqual(self.terminal, before)

    def test_legacy_schema_and_unproven_cleanup_do_not_read_database(self):
        self.assertEqual(core.valid_provider_failure_diagnostic(self.diagnostic, self.terminal), self.diagnostic)
        for field in ("cleanup_pass", "engine_reaped", "no_live_descendants"):
            with self.subTest(field=field), patch.object(durable_recovery, "_database", side_effect=AssertionError("no DB read")):
                terminal = deepcopy(self.terminal)
                if field == "cleanup_pass":
                    terminal[field] = False
                else:
                    terminal["cleanup"][field] = False
                value = core.provider_failure_diagnostic_with_snapshot(self.diagnostic, terminal, self.run)
                self.assertNotIn("stream_failure_snapshot", value or {})

    def test_valid_snapshot_is_bound_and_never_changes_failure_authority(self):
        before = deepcopy(self.terminal)
        value = self.collect()
        evidence = value["stream_failure_snapshot"]
        self.assertEqual(evidence["snapshot"], self.snapshot)
        self.assertEqual(evidence["session_id"], self.session)
        self.assertEqual((evidence["output_event_seq"], evidence["failure_event_seq"], evidence["last_event_seq"]), (2, 3, 3))
        self.assertEqual(core.valid_provider_failure_diagnostic(value, self.terminal), value)
        self.assertEqual(self.terminal, before)
        self.assertEqual(value["effect_outcome"], "UNKNOWN")
        self.assertNotIn("retry_allowed", value)
        self.assertEqual(self.terminal["first_typed_blocker"], "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED")

    def test_invalid_snapshot_types_shapes_counters_and_claims_are_omitted(self):
        original = deepcopy(self.snapshot)
        mutations = [("event_count", True), ("event_count", -1), ("event_count", 2**63),
            ("pending_bytes", False), ("pending_bytes", -1), ("pending_bytes", 2**63),
            ("completed_seen", 0), ("completion_valid", None), ("completed_frame_ended", "false"),
            ("command_effect_started", None), ("runtime_id", None), ("runtime_id", "foreign"),
            ("request_id", "secret\nvalue"), ("request_id", "x" * 257),
            ("schema_version", "foreign"), ("retry_allowed", True), ("completion_valid", True)]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                self.snapshot = {**original, key: value}
                self.update_event(2, json.dumps(self.output()))
                self.assert_omitted()
        self.snapshot = original
        for raw in ("{", "[]", '{"output":{},"output":{}}'):
            with self.subTest(raw=raw):
                self.update_event(2, raw)
                self.assert_omitted()

    def test_wrong_session_runtime_sequence_and_stale_state_are_omitted(self):
        changes = ["UPDATE sessions SET next_context_sequence=2", "UPDATE sessions SET retained_from_sequence=1",
            "UPDATE runtime_locations SET session_id='foreign'", "UPDATE runtimes SET session_id='foreign'",
            "UPDATE runtimes SET lease_active=1", "UPDATE runtimes SET terminal=0",
            "UPDATE runtimes SET last_event_seq=4", "UPDATE runtime_events SET event_seq=-1 WHERE event_seq=1",
            "UPDATE runtime_events SET event_seq=2 WHERE event_seq=1",
            "UPDATE runtime_events SET runtime_id='foreign' WHERE event_seq=2",
            "UPDATE session_context_records SET sequence=2"]
        for sql in changes:
            with self.subTest(sql=sql):
                backup = self.root / "db.backup"
                index_backup = self.root / "index.backup"
                backup.write_bytes(self.db.read_bytes())
                index_backup.write_bytes(self.index.read_bytes())
                target = self.index if "runtime_locations" in sql else self.db
                with sqlite3.connect(target) as db:
                    db.execute(sql)
                self.assert_omitted()
                self.db.write_bytes(backup.read_bytes())
                self.index.write_bytes(index_backup.read_bytes())
        self.update_event(2, json.dumps({**self.output(), "runtime_id": "foreign"}))
        self.assert_omitted()

    def test_duplicate_conflicting_outputs_and_duplicate_locations_are_omitted(self):
        with sqlite3.connect(self.index) as db:
            db.execute("INSERT INTO runtime_locations VALUES(?,?,?)", (self.runtime, self.session, str(self.db)))
        self.assert_omitted()
        with sqlite3.connect(self.index) as db:
            db.execute("DELETE FROM runtime_locations WHERE rowid=(SELECT MAX(rowid) FROM runtime_locations)")
        for changed in (False, True):
            with self.subTest(changed=changed), sqlite3.connect(self.db) as db:
                db.execute("DELETE FROM runtime_events WHERE event_seq>=3")
                db.execute("UPDATE runtimes SET last_event_seq=4")
                output = self.output()
                if changed:
                    output["output"]["provider_stream_failure"] = {**self.snapshot, "event_count": 7}
                db.executemany("INSERT INTO runtime_events VALUES(?,?,?)", [
                    (self.runtime, 3, json.dumps(output)), (self.runtime, 4, json.dumps(self.failure()))])
            self.assert_omitted()

    def test_symlink_and_database_drift_are_omitted(self):
        real = self.index.with_suffix(".real")
        self.index.rename(real)
        self.index.symlink_to(real)
        self.assert_omitted()
        self.index.unlink()
        real.rename(self.index)
        original = durable_recovery._database

        @contextmanager
        def drift(run, relative, reader):
            with original(run, relative, reader) as opened:
                yield opened
                if relative == "execution-state/session_log/index.sqlite3":
                    with self.index.open("ab") as output:
                        output.write(b"x")

        with patch.object(durable_recovery, "_database", side_effect=drift):
            self.assert_omitted()

    def test_legacy_terminal_inspection_does_not_acquire_new_optional_field(self):
        value = self.collect()
        self.assertIn("stream_failure_snapshot", value)
        with patch.object(durable_recovery, "_database", side_effect=AssertionError("legacy must not read")):
            legacy = core.provider_failure_diagnostic_with_snapshot(self.diagnostic, self.terminal, self.run,
                                                                   require_existing=True)
            for invalid in (None, [], False):
                self.assertEqual(core.provider_failure_diagnostic_with_snapshot(
                    self.diagnostic, {**self.terminal, "provider_failure_diagnostic": invalid}, self.run,
                    require_existing=True), self.diagnostic)
        self.assertEqual(legacy, self.diagnostic)

    def test_inspection_reconstruction_and_summary_preserve_snapshot_sha(self):
        value = self.collect()
        terminal = {**self.terminal, "provider_failure_diagnostic": value}
        reconstructed = core.provider_failure_diagnostic_with_snapshot(
            self.diagnostic, terminal, self.run, require_existing=True)
        self.assertEqual(reconstructed, value)
        projected = {**terminal, "result_inspection": {"terminal_sha256": "d" * 64,
            "provider_failure_diagnostic_sha256": core.caller._canonical_sha256(reconstructed)}}
        with patch.object(inspector, "inspect", side_effect=AssertionError("summary must not inspect")):
            summary = inspector.summarize_terminal(projected, self.root, self.run.name)
        self.assertEqual(summary["provider_failure_diagnostic"], value)
        self.assertEqual(summary["first_typed_blocker"], terminal["first_typed_blocker"])
        self.update_event(2, "{")
        reconstructed = core.provider_failure_diagnostic_with_snapshot(
            self.diagnostic, terminal, self.run, require_existing=True)
        self.assertEqual(reconstructed, self.diagnostic)
        projected["result_inspection"]["provider_failure_diagnostic_sha256"] = (
            core.caller._canonical_sha256(reconstructed))
        self.assertIsNone(inspector._inspected_failure_diagnostic(projected))
        self.assertEqual(terminal["first_typed_blocker"], "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED")


if __name__ == "__main__":
    unittest.main()
