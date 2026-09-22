# SPDX-License-Identifier: MIT
"""Synthetic sealed evidence tests; no engine, provider or real task writes."""

import copy
import fcntl
import gzip
import hashlib
import io
import json
import os
from unittest.mock import patch

from codex_collaboration_harness.core import canonical_sha256
from codex_collaboration_harness.graph_entry import GraphEntryError, MAX_RESPONSE, _encoded, main, verify_context
from codex_collaboration_harness.graph_retention import _read, _safe_id, _validate_pair, read_completed_call
from test_graph_entry import GraphFixture


class ResultReadTests(GraphFixture):
    def setUp(self):
        super().setUp()
        self.task, self.call = "fixture-task", "read-1"
        self.payload["request"]["expires_at_unix"] = 1
        self.digest = canonical_sha256(self.payload["request"])
        self.key = canonical_sha256([self.task, self.call])
        self.state = self.artifacts / "tura-graph" / self.task
        self.state.mkdir(parents=True)
        node = f"{self.task}:{self.call}:step1"
        prefix = ".tura/run/command_receipts/"
        recovery = {"format": "gzip-terminal-bundle-v1", "path": str(self.state / (self.key + ".json.gz"))}
        self.result = {"status": "completed", "task_id": self.task, "call_id": self.call,
                       "request_digest": self.digest, "replayed": False,
                       "process_closeout_required": False, "recovery_artifact": recovery,
                       "graph": {"results": [{"id": "step1", "success": True, "output": "fixture only"}]}}
        journal = {"schema_version": "codex_tura_graph_journal_v1", "closeout_version": 1,
                   "supervision_version": 1, "status": "completed", "task_id": self.task,
                   "call_id": self.call, "request_digest": self.digest,
                   "nodes": {node: {"status": "completed"}}, "result": copy.deepcopy(self.result),
                   "process_scope": {"scope": "per-call-inherited-seatbelt-signal-boundary",
                                     "engine_reaped": True, "no_live_descendants": True,
                                     "cleanup_error": None, "engine_exit_code": 0,
                                     "descendant_signals": 0, "effect_outcome_inferred_from_process_exit": False}}
        batch = {"schema_version": "tura_command_run_batch_admission_v1", "state": "finished",
                 "execution_id": f"{self.task}:{self.call}", "call_ids": [node], "accepted_call_ids": [node]}
        closeout = {"call_id": node, "process_reaped": True, "process_group_empty": True,
                    "reconcile_required": False}
        claim = {**closeout, "schema_version": "tura_command_execution_claim_v1", "state": "completed"}
        receipt = {**closeout, "schema_version": "tura_command_terminal_receipt_v1",
                   "terminal_state": "completed", "outcome": "known", "exit_code": 0,
                   "termination_proven": True}
        self.objects = [journal, batch, claim, receipt]
        self.paths = [self.key + ".json", prefix + _safe_id(f"{self.task}:{self.call}") + ".batch-admission",
                      prefix + _safe_id(node) + ".claim.json", prefix + _safe_id(node) + ".json"]
        self.publish_fixture()

    def publish_fixture(self):
        journal = self.objects[0]
        journal["result_digest"] = canonical_sha256(journal["result"])
        members = []
        for path, obj in zip(self.paths, self.objects):
            raw = _encoded(obj)
            members.append({"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                            "content": raw.decode()})
        bundle = {"schema_version": "codex_tura_graph_terminal_bundle_v1", "members": members}
        bundle["semantic_sha256"] = canonical_sha256(bundle)
        guard = {"schema_version": "codex_tura_graph_journal_v1", "closeout_version": 1,
                 "task_id": self.task, "call_id": self.call, "request_digest": self.digest,
                 "status": "sealed", "archive_semantic_sha256": bundle["semantic_sha256"],
                 "recovery_artifact": self.result["recovery_artifact"]}
        (self.state / (self.key + ".json")).write_bytes(_encoded(guard))
        (self.state / (self.key + ".json.gz")).write_bytes(gzip.compress(_encoded(bundle), mtime=0))

    def read(self):
        return read_completed_call(self.artifacts, self.task, self.call, self.digest)

    def arguments(self):
        return ["read-result", "--call-id", self.call, "--request-digest", self.digest,
                "--engine", str(self.settings.engine), "--engine-sha256", "0" * 64,
                "--dcf-root", str(self.workspace), "--artifact-root", str(self.artifacts)]

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in self.state.iterdir() if p.is_file()}

    def cli(self, arguments=None):
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": self.task}),
              patch("codex_collaboration_harness.graph_entry.DcfBoundary") as dcf,
              patch("codex_collaboration_harness.graph_entry.execute") as engine,
              patch("sys.stdin", object()), patch("sys.stdout", new_callable=io.StringIO) as output):
            status = main(arguments or self.arguments())
        dcf.assert_not_called()
        engine.assert_not_called()
        return status, output.getvalue()

    def test_expired_completed_result_cli_deterministic_and_no_mutation(self):
        with self.assertRaisesRegex(GraphEntryError, "BUDGET_INVALID"):
            verify_context(self.payload, self.settings, self.verify_fixture)
        before = self.snapshot()
        first, second = self.cli(), self.cli()
        self.assertEqual(first, second)
        self.assertEqual(first[0], 0)
        result = json.loads(first[1])
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(result["recorded_status"], "completed")
        self.assertEqual(result["full_result_sha256"], canonical_sha256(self.result))
        self.assertFalse(result["execution_started"])
        self.assertFalse(result["task_terminal_verified"])
        self.assertEqual(result["source_mutation_count"], 0)
        self.assertEqual(self.snapshot(), before)

    def test_native_identity_checked_before_artifact_access(self):
        for environment, flags in (({}, []), ({"CODEX_THREAD_ID": self.task}, ["--caller-task-id", "foreign"])):
            with (patch.dict(os.environ, environment, clear=True),
                  patch("codex_collaboration_harness.graph_retention.read_completed_call") as reader,
                  patch("sys.stdout", new_callable=io.StringIO)):
                self.assertEqual(main(self.arguments() + flags), 2)
            reader.assert_not_called()

    def test_read_only_entry_needs_only_artifact_root_not_execution_configuration(self):
        base = ["read-result", "--call-id", self.call, "--request-digest", self.digest,
                "--config", str(self.root / "missing.json")]
        status, raw = self.cli(base + ["--artifact-root", str(self.artifacts)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(raw)["status"], "retrieved")
        config = self.root / "read-only.json"
        config.write_text(json.dumps({"artifact_root": str(self.artifacts)}))
        status, _ = self.cli(base[:-1] + [str(config)])
        self.assertEqual(status, 0)

    def test_invalid_identity_and_request_mismatch_rejected(self):
        for values in (("../foreign", self.call, self.digest), (self.task, "../foreign", self.digest),
                       (self.task, None, self.digest), (self.task, self.call, "not-a-digest")):
            with patch("os.open") as opened:
                with self.assertRaisesRegex(GraphEntryError, "IDENTITY_INVALID"):
                    read_completed_call(self.artifacts, *values)
            opened.assert_not_called()
        with self.assertRaisesRegex(GraphEntryError, "REQUEST_MISMATCH"):
            read_completed_call(self.artifacts, self.task, self.call, "0" * 64)

    def test_only_exact_pair_read_unsettled_sibling_preserved(self):
        (self.state / "unsettled.tmp").write_bytes(b"not our call")
        before = self.snapshot()
        with patch("codex_collaboration_harness.graph_retention._read", wraps=_read) as reader:
            self.assertEqual(self.read()["status"], "retrieved")
        self.assertEqual({call.args[1] for call in reader.call_args_list},
                         {self.key + ".json", self.key + ".json.gz"})
        self.assertEqual(self.snapshot(), before)

    def test_missing_pair_rejected_but_sibling_writer_does_not_block_read(self):
        for suffix in (".json", ".json.gz"):
            path = self.state / (self.key + suffix)
            raw = path.read_bytes()
            path.unlink()
            with self.assertRaisesRegex(GraphEntryError, "SEALED_PAIR_MISSING"):
                self.read()
            path.write_bytes(raw)
        fd = os.open(self.state, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.snapshot()
            self.assertEqual(self.read()["status"], "retrieved")
            self.assertEqual(self.snapshot(), before)
            other = os.open(self.state, os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(other)
        finally:
            os.close(fd)

    def test_self_consistent_false_completion_and_digest_corruption_rejected(self):
        original = copy.deepcopy(self.objects)
        for mutate in (lambda o: o[0].update(task_id="foreign"),
                       lambda o: o[0].update(status="interrupted"),
                       lambda o: o[0]["process_scope"].update(no_live_descendants=False),
                       lambda o: o[0]["result"].update(process_closeout_required=True),
                       lambda o: o[3].update(outcome="unknown"),
                       lambda o: o[0]["result"]["recovery_artifact"].update(path="/foreign")):
            self.objects = copy.deepcopy(original)
            mutate(self.objects)
            self.publish_fixture()
            with self.assertRaises(GraphEntryError):
                self.read()
        self.objects = original
        self.publish_fixture()
        path = self.state / (self.key + ".json.gz")
        bundle = json.loads(gzip.decompress(path.read_bytes()))
        bundle["members"][0]["content"] += " "
        path.write_bytes(gzip.compress(_encoded(bundle)))
        with self.assertRaisesRegex(GraphEntryError, "DIGEST_MISMATCH"):
            self.read()

    def test_links_and_path_replacement_rejected(self):
        path = self.state / (self.key + ".json")
        saved = self.root / "saved"
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises(OSError):
            self.read()
        path.unlink()
        os.link(saved, path)
        with self.assertRaisesRegex(GraphEntryError, "FILE_IDENTITY_INVALID"):
            self.read()
        path.unlink()
        saved.rename(path)
        original = _validate_pair
        def replace_pair(*args):
            result = original(*args)
            replacement = self.root / "replacement"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            return result
        with patch("codex_collaboration_harness.graph_retention._validate_pair", side_effect=replace_pair):
            with self.assertRaisesRegex(GraphEntryError, "SNAPSHOT_CHANGED"):
                self.read()

    def test_task_symlink_and_oversized_expansion_rejected(self):
        moved = self.root / "moved"
        self.state.rename(moved)
        self.state.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            self.read()
        self.state.unlink()
        moved.rename(self.state)
        self.objects[0]["result"]["graph"]["padding"] = "x" * 20000
        self.publish_fixture()
        with patch("codex_collaboration_harness.graph_retention.MAX_TASK_BYTES", 10000):
            with self.assertRaisesRegex(GraphEntryError, "DECOMPRESSION_LIMIT"):
                self.read()

    def test_large_output_is_bounded_without_losing_raw_result_digest(self):
        self.objects[0]["result"]["graph"]["padding"] = "x" * MAX_RESPONSE
        self.publish_fixture()
        status, raw = self.cli()
        self.assertEqual(status, 0)
        self.assertLessEqual(len(raw.encode()), MAX_RESPONSE)
        result = json.loads(raw)
        self.assertTrue(result["output_truncated"])
        self.assertEqual(result["full_result_sha256"], canonical_sha256(self.objects[0]["result"]))
        self.assertNotIn("graph", result)

    def test_unprojectable_result_is_not_relabelled_as_success(self):
        self.objects[0]["result"]["diagnostics"] = "x" * MAX_RESPONSE
        self.publish_fixture()
        status, raw = self.cli()
        self.assertEqual(status, 2)
        self.assertLessEqual(len(raw.encode()), MAX_RESPONSE)
        result = json.loads(raw)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"], "GRAPH_RESPONSE_BUDGET_EXCEEDED")
        self.assertEqual(result["recorded_status"], "completed")
