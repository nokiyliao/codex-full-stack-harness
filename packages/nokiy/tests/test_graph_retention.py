# SPDX-License-Identifier: MIT
"""Retirement preparation uses isolated fixtures, never Native task authority."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from codex_collaboration_harness.core import canonical_sha256
from codex_collaboration_harness.graph_entry import GraphEntryError, execute, main
from codex_collaboration_harness.graph_retention import RetirementCandidate, _decode, _inflate, prepare_task_retirement
from test_graph_entry import GraphFixture


class PreparationEntryTests(GraphFixture):
    def arguments(self) -> list[str]:
        return ["plan-retirement", "--engine", str(self.settings.engine), "--engine-sha256", "0" * 64,
                "--dcf-root", str(self.workspace), "--artifact-root", str(self.artifacts)]

    def test_read_only_plan_needs_no_dcf_model_or_stdin(self) -> None:
        report = {"status": "prepared_not_admitted", "task_terminal_verified": False}
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}),
              patch("codex_collaboration_harness.graph_entry.DcfBoundary") as dcf,
              patch("codex_collaboration_harness.graph_retention.prepare_task_retirement",
                    return_value=RetirementCandidate(report, b"never emit raw archive")) as prepare,
              patch("sys.stdin", object()), patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(main(self.arguments()), 0)
        prepare.assert_called_once_with(self.artifacts, "fixture-task")
        dcf.assert_not_called()
        value = json.loads(output.getvalue())
        self.assertEqual(value["status"], "prepared_not_admitted")
        self.assertFalse(value["execution_started"])
        self.assertNotIn("archive", value)

    def test_missing_or_foreign_native_caller_does_not_inspect_artifacts(self) -> None:
        for environment, override in (({}, []), ({"CODEX_THREAD_ID": "fixture-task"},
                                                  ["--caller-task-id", "foreign"])):
            with (patch.dict(os.environ, environment, clear=True),
                  patch("codex_collaboration_harness.graph_retention.prepare_task_retirement") as prepare,
                  patch("sys.stdout", new_callable=io.StringIO) as output):
                self.assertEqual(main(self.arguments() + override), 2)
            prepare.assert_not_called()
            self.assertIn("GRAPH_NATIVE_CALLER_ENV_", json.loads(output.getvalue())["error"])

    def test_plan_needs_no_execution_configuration(self) -> None:
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}),
              patch("codex_collaboration_harness.graph_retention.prepare_task_retirement",
                    return_value=RetirementCandidate({"status": "prepared_not_admitted"}, b"")) as prepare,
              patch("sys.stdin", object()), patch("sys.stdout", new_callable=io.StringIO)):
            self.assertEqual(main(["plan-retirement", "--artifact-root", str(self.artifacts),
                                   "--config", str(self.root / "missing.json")]), 0)
        prepare.assert_called_once_with(self.artifacts, "fixture-task")

    def test_path_traversal_rejected_before_any_read(self) -> None:
        for identity in ("../other", "", "foreign/task"):
            with self.assertRaisesRegex(GraphEntryError, "TASK_ID_INVALID"):
                prepare_task_retirement(self.artifacts, identity)

    def test_duplicate_json_and_nonfinite_constants_are_rejected(self) -> None:
        for data in (b'{"status":1,"status":2}', b'{"status":NaN}'):
            with self.assertRaises(GraphEntryError):
                _decode(data)

    def test_decompression_is_bounded(self) -> None:
        with self.assertRaisesRegex(GraphEntryError, "DECOMPRESSION_LIMIT"):
            _inflate(gzip.compress(b"x" * 1000), 10)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("NOKIY_GRAPH_ENGINE"),
                     "requires admitted macOS test engine")
class RetirementTests(GraphFixture):
    def setUp(self) -> None:
        super().setUp()
        self.result = asyncio.run(execute(self.payload, self.settings, self.verify_fixture))
        self.assertEqual(self.result["status"], "completed")
        self.state = self.artifacts / "tura-graph/fixture-task"
        self.key = canonical_sha256(["fixture-task", "read-1"])

    def snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.state)): p.read_bytes()
                for p in self.state.rglob("*") if p.is_file()}

    def prepare(self):
        return prepare_task_retirement(self.artifacts, "fixture-task")

    def rewrite_bundle(self, change) -> None:
        path = self.state / (self.key + ".json.gz")
        bundle = json.loads(gzip.decompress(path.read_bytes()))
        change(bundle)
        bundle["semantic_sha256"] = canonical_sha256({
            k: v for k, v in bundle.items() if k != "semantic_sha256"})
        path.write_bytes(gzip.compress(json.dumps(bundle).encode(), mtime=0))
        guard = self.state / (self.key + ".json")
        value = json.loads(guard.read_bytes())
        value["archive_semantic_sha256"] = bundle["semantic_sha256"]
        guard.write_text(json.dumps(value))

    def rewrite_journal(self, mutate) -> None:
        def change(bundle):
            member = bundle["members"][0]
            journal = json.loads(member["content"])
            mutate(journal)
            journal["result_digest"] = canonical_sha256(journal["result"])
            raw = json.dumps(journal).encode()
            member.update(content=raw.decode(), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        self.rewrite_bundle(change)

    def test_deterministic_lossless_no_mutation_no_terminal_inference(self) -> None:
        before = self.snapshot()
        first, second = self.prepare(), self.prepare()
        self.assertEqual(first, second)
        self.assertEqual(self.snapshot(), before)
        snapshot = json.loads(gzip.decompress(first.archive))
        restored = {m["path"]: base64.b64decode(m["content_base64"], validate=True)
                    for m in snapshot["files"]}
        self.assertEqual(restored, before)
        self.assertEqual(first.report["source_file_count"], 2)
        self.assertFalse(first.report["task_terminal_verified"])
        self.assertFalse(first.report["task_retirement_authorized"])
        self.assertEqual(first.report["reclaimed_logical_bytes"], 0)
        self.assertEqual(first.report["persistent_artifacts_created"], 0)

    def test_multiple_calls_one_complete_snapshot(self) -> None:
        self.payload["request"]["call_id"] = "read-2"
        result = asyncio.run(execute(self.payload, self.settings, self.verify_fixture))
        self.assertEqual(result["status"], "completed")
        prepared = self.prepare()
        self.assertEqual(prepared.report["sealed_call_count"], 2)
        self.assertEqual(prepared.report["source_file_count"], 4)

    def test_busy_owner_is_preserved(self) -> None:
        before = self.snapshot()
        lock = os.open(self.state, os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(GraphEntryError, "OWNER_BUSY"):
                self.prepare()
        finally:
            os.close(lock)
        self.assertEqual(self.snapshot(), before)

    def test_missing_guard_or_archive_is_not_retirable(self) -> None:
        for suffix in (".json", ".json.gz"):
            path = self.state / (self.key + suffix)
            raw = path.read_bytes()
            path.unlink()
            with self.assertRaisesRegex(GraphEntryError, "INCOMPLETE_SEALED_PAIR"):
                self.prepare()
            path.write_bytes(raw)

    def test_unknown_staging_and_unsettled_files_are_preserved(self) -> None:
        for name in ("unsettled.tmp", ".tura/run/command_receipts/unsettled.json", "unknown"):
            path = self.state / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unknown")
            with self.assertRaisesRegex(GraphEntryError, "UNSETTLED_OR_UNKNOWN_ENTRY"):
                self.prepare()
            self.assertEqual(path.read_bytes(), b"unknown")
            path.unlink()

    def test_symlink_and_hardlink_are_rejected(self) -> None:
        path = self.state / (self.key + ".json")
        saved = self.root / "guard"
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaisesRegex(GraphEntryError, "UNSETTLED_OR_UNKNOWN_ENTRY"):
            self.prepare()
        path.unlink()
        os.link(saved, path)
        with self.assertRaisesRegex(GraphEntryError, "FILE_IDENTITY_INVALID"):
            self.prepare()

    def test_task_and_parent_symlinks_are_rejected(self) -> None:
        moved = self.root / "moved"
        self.state.rename(moved)
        self.state.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            self.prepare()
        self.state.unlink()
        moved.rename(self.state)
        parent = self.state.parent
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(GraphEntryError, "DIRECTORY_INVALID"):
            self.prepare()

    def test_foreign_identity_and_unproven_closeout_are_rejected(self) -> None:
        self.rewrite_journal(lambda journal: journal.update(task_id="foreign"))
        with self.assertRaisesRegex(GraphEntryError, "TERMINAL_IDENTITY_INVALID"):
            self.prepare()

    def test_process_and_terminal_state_are_not_inferred(self) -> None:
        originals = self.snapshot()
        for mutate in (lambda j: j.update(supervision_version=0),
                       lambda j: j["process_scope"].update(no_live_descendants=False),
                       lambda j: j["result"].update(process_closeout_required=True),
                       lambda j: j["process_scope"].update(engine_exit_code=False)):
            self.rewrite_journal(mutate)
            with self.assertRaisesRegex(GraphEntryError, "PROCESS_CLOSEOUT_UNPROVEN"):
                self.prepare()
            for name, raw in originals.items():
                (self.state / name).write_bytes(raw)

    def test_member_omission_and_path_drift_are_rejected(self) -> None:
        self.rewrite_bundle(lambda b: b["members"].pop())
        with self.assertRaisesRegex(GraphEntryError, "MEMBERS_INVALID"):
            self.prepare()

    def test_guard_drift_is_rejected(self) -> None:
        path = self.state / (self.key + ".json")
        guard = json.loads(path.read_bytes())
        guard["status"] = "interrupted"
        path.write_text(json.dumps(guard))
        with self.assertRaisesRegex(GraphEntryError, "ROLLBACK_GUARD_MISMATCH"):
            self.prepare()

    def test_budget_failure_does_not_prune(self) -> None:
        before = self.snapshot()
        for limit in ("MAX_TASK_BYTES", "MAX_TASK_FILES"):
            with patch("codex_collaboration_harness.graph_retention." + limit, 1):
                with self.assertRaisesRegex(GraphEntryError, "SNAPSHOT_LIMIT"):
                    self.prepare()
        self.assertEqual(self.snapshot(), before)

    def test_snapshot_changed_while_packing_is_rejected(self) -> None:
        original = gzip.compress
        def drift(data, **kwargs):
            (self.state / "late-evidence").write_bytes(b"retain")
            return original(data, **kwargs)
        with patch("codex_collaboration_harness.graph_retention.gzip.compress", side_effect=drift):
            with self.assertRaisesRegex(GraphEntryError, "UNSETTLED_OR_UNKNOWN_ENTRY"):
                self.prepare()
        self.assertEqual((self.state / "late-evidence").read_bytes(), b"retain")

    def test_self_consistent_unsafe_member_path_is_rejected(self) -> None:
        self.rewrite_bundle(lambda b: b["members"][1].update(path="../foreign"))
        with self.assertRaisesRegex(GraphEntryError, "MEMBER_PATH_INVALID"):
            self.prepare()

    def test_self_consistent_unsettled_node_is_rejected(self) -> None:
        def change(bundle):
            member = bundle["members"][3]
            value = json.loads(member["content"])
            value.update(outcome="unknown", reconcile_required=True)
            raw = json.dumps(value).encode()
            member.update(content=raw.decode(), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        self.rewrite_bundle(change)
        with self.assertRaisesRegex(GraphEntryError, "NODE_UNSETTLED"):
            self.prepare()

    @unittest.skipUnless(os.environ.get("NOKIY_GRAPH_ROLLBACK_ENGINE"), "requires rollback engine")
    def test_file_tombstone_blocks_both_engines_without_effects(self) -> None:
        # Fixture proof only, NOT an implementation of atomic task retirement.
        candidate = self.prepare()
        retained = self.root / "preserved-state"
        self.state.rename(retained)
        self.state.write_bytes(json.dumps(candidate.report).encode())
        for engine in (str(self.settings.engine), os.environ["NOKIY_GRAPH_ROLLBACK_ENGINE"]):
            response = subprocess.run([engine, "--state-dir", str(self.state)],
                                      input=json.dumps(self.payload["request"]), text=True,
                                      capture_output=True, timeout=10,
                                      env={"PATH": "/usr/bin:/bin", "HOME": str(self.root)})
            result = json.loads(response.stdout)
            self.assertEqual(result["status"], "blocked")
            self.assertRegex(result.get("error", ""), "File exists|Not a directory")
            self.assertNotEqual(result.get("replayed"), True)
        self.assertEqual((self.workspace / "src/value.txt").read_text(), "before\n")
        self.assertEqual(len(list(retained.glob("*.json*"))), 2)


if __name__ == "__main__":
    unittest.main()
