import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_collaboration_harness import session_log_retention as retention

DIGEST = "c" * 64
SESSION = "full-" + DIGEST


def usage_record(runtime, tokens):
    return json.dumps({"runtime_id": runtime, "type": "runtime_usage",
                       "usage": {"input_tokens": tokens, "cached_input_tokens": 0,
                                 "output_tokens": 7, "reasoning_tokens": 2}})


class SessionLogRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name) / ("tura_embedded_" + DIGEST)
        self.log = self.run / "execution-state" / "session_log"
        self.db = self.log / "workspaces" / ("a" * 64) / ".tura" / "session_log.sqlite3"
        self.db.parent.mkdir(parents=True)
        (self.run / "execution-state" / "command_receipts").mkdir()
        with sqlite3.connect(self.log / "index.sqlite3") as index:
            index.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, workspace_db_path TEXT)")
            index.execute("INSERT INTO sessions VALUES (?, ?)", (SESSION, str(self.db)))
        self.records = [usage_record("runtime-one", 100), usage_record("runtime-two", 200)]
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE session_context_records (session_id TEXT, sequence INTEGER, record_json TEXT)")
            db.execute("CREATE TABLE runtimes (runtime_id TEXT PRIMARY KEY, session_id TEXT)")
            db.execute("CREATE TABLE runtime_events (runtime_id TEXT, event_seq INTEGER, event_json TEXT)")
            rows = [(SESSION, 0, json.dumps({"type": "tool_result", "output": "x" * 5000}))]
            rows += [(SESSION, i + 1, raw) for i, raw in enumerate(self.records)]
            db.executemany("INSERT INTO session_context_records VALUES (?, ?, ?)", rows)
            for runtime in ("runtime-one", "runtime-two"):
                db.execute("INSERT INTO runtimes VALUES (?, ?)", (runtime, SESSION))
                db.executemany("INSERT INTO runtime_events VALUES (?, ?, ?)", [
                    (runtime, 1, json.dumps({"event": "call_started", "called_at": "2026-10-26T15:00:00Z"})),
                    (runtime, 2, json.dumps({"event": "input_captured", "input": "y" * 5000})),
                    (runtime, 3, json.dumps({"event": "first_token_received", "first_token_at": "2026-10-26T15:00:02Z"})),
                    (runtime, 4, json.dumps({"event": "runtime_finished", "finished_at": "2026-10-26T15:00:03Z",
                                             "usage": {"output_tokens": 7, "reasoning_tokens": 2}}))])
        self.terminal = {"status": "RESULT_AVAILABLE", "cleanup_pass": True, "request_sha256": DIGEST,
                         "command_evidence_artifact": {"path": "command-evidence.json"}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_sealed_run_keeps_usage_and_timing_then_removes_database(self):
        self.assertEqual(retention.retire(self.run, self.terminal), "retired")
        self.assertFalse(self.log.exists())
        self.assertTrue((self.run / "execution-state" / "command_receipts").is_dir())
        calls = retention.load(self.run / retention.SIDECAR_NAME, SESSION)
        self.assertEqual(calls["usage_records"], self.records)
        first = calls["runtimes"][0]
        self.assertEqual(first["runtime_id"], "runtime-one")
        self.assertEqual(first["events"], [[1, "call_started", "2026-10-26T15:00:00Z"],
                                           [2, "input_captured", None],
                                           [3, "first_token_received", "2026-10-26T15:00:02Z"],
                                           [4, "runtime_finished", "2026-10-26T15:00:03Z"]])
        self.assertEqual(first["finished_usage"], {"output_tokens": 7, "reasoning_tokens": 2})
        self.assertNotIn("y" * 100, (self.run / retention.SIDECAR_NAME).read_text())

    def test_terminals_that_may_need_the_database_keep_it(self):
        snapshot = {"provider_failure_diagnostic": {"stream_failure_snapshot": {}}}
        for change in ({"cleanup_pass": False}, {"command_evidence_artifact": None},
                       {"status": "BLOCKED", **snapshot}, {"status": "UNKNOWN"}, {"request_sha256": None}):
            with self.subTest(change=change):
                self.assertEqual(retention.retire(self.run, {**self.terminal, **change}), "kept_required")
                self.assertTrue(self.db.is_file())
                self.assertFalse((self.run / retention.SIDECAR_NAME).exists())

    def test_blocked_terminal_with_command_evidence_is_disposable(self):
        blocked = {**self.terminal, "status": "BLOCKED", "provider_failure_diagnostic": {"event_index": 3}}
        self.assertTrue(retention.disposable(blocked))

    def test_unproven_summary_keeps_database(self):
        with sqlite3.connect(self.log / "index.sqlite3") as index:
            index.execute("UPDATE sessions SET workspace_db_path=?", (str(Path(self.tmp.name) / "elsewhere.db"),))
        (Path(self.tmp.name) / "elsewhere.db").write_bytes(self.db.read_bytes())
        self.assertEqual(retention.retire(self.run, self.terminal), "kept_unproven")
        self.assertTrue(self.db.is_file())
        self.assertFalse((self.run / retention.SIDECAR_NAME).exists())

    def test_invalid_existing_sidecar_keeps_database(self):
        (self.run / retention.SIDECAR_NAME).write_text(json.dumps({"schema_version": "other"}))
        self.assertEqual(retention.retire(self.run, self.terminal), "kept_unproven")
        self.assertTrue(self.db.is_file())

    def test_valid_existing_sidecar_is_reused(self):
        self.assertEqual(retention.retire(self.run, self.terminal), "retired")
        self.log.mkdir(parents=True)
        self.assertEqual(retention.retire(self.run, self.terminal), "retired")
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()

