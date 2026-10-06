import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "paired_long_task_eval.py"
SPEC = importlib.util.spec_from_file_location("paired_long_task_eval", SCRIPT)
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)
TASK_CONTENT = b"one frozen task"
TASK_CONTENT_SHA256 = hashlib.sha256(TASK_CONTENT).hexdigest()


def observation(arm):
    start, end, verified = (
        ("2026-09-01T10:00:00+00:00", "2026-09-01T10:10:00+00:00",
         "2026-09-01T10:11:00+00:00") if arm == "baseline" else
        ("2026-09-01T11:00:00+00:00", "2026-09-01T11:09:00+00:00",
         "2026-09-01T11:10:00+00:00")
    )
    total = 120 if arm == "baseline" else 100
    return {
        "schema_version": evaluator.OBSERVATION_SCHEMA,
        "task_id": "frozen-task-1", "task_sha256": TASK_CONTENT_SHA256,
        "arm": arm, "model": "codex/gpt-6-astra", "effort": "high",
        "run_id": f"run-{arm}", "executor_id": f"executor-{arm}",
        "output_sha256": hashlib.sha256(f"{arm} output".encode()).hexdigest(),
        "run_status": "COMPLETE", "started_at": start, "ended_at": end,
        "verifier": {"source": "independent", "verifier_id": "reviewer-1",
                     "status": "PASS", "verified_at": verified},
        "usage": {"status": "KNOWN", "input_tokens": total - 20,
                  "output_tokens": 20, "total_tokens": total},
    }


def receipt(observed):
    return {
        "schema_version": evaluator.RECEIPT_SCHEMA,
        **{key: observed[key] for key in
           ("task_id", "task_sha256", "model", "effort", "arm", "run_id",
            "executor_id", "output_sha256")},
        **observed["verifier"],
    }


class PairedLongTaskEvalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.baseline = observation("baseline")
        self.candidate = observation("candidate")
        self.task_content_path = self.root / "task-content.txt"
        self.task_content_path.write_bytes(TASK_CONTENT)

    def _write(self, name, value):
        path = self.root / name
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def _inputs(self, receipt_pins=None):
        baseline_path, baseline_sha = self._write("baseline.json", self.baseline)
        candidate_path, candidate_sha = self._write("candidate.json", self.candidate)
        task = {
            "schema_version": evaluator.TASK_SCHEMA,
            "task_id": "frozen-task-1", "task_sha256": TASK_CONTENT_SHA256,
            "model": "codex/gpt-6-astra", "effort": "high",
            "arms": {"baseline": baseline_sha, "candidate": candidate_sha},
        }
        if receipt_pins is not None:
            task["verifier_receipts"] = receipt_pins
        task_path, task_sha = self._write("task.json", task)
        return task_path, task_sha, baseline_path, candidate_path

    def _evidence_inputs(self, baseline_receipt=None, candidate_receipt=None):
        baseline_receipt_path, baseline_receipt_sha = self._write(
            "baseline-receipt.json", baseline_receipt or receipt(self.baseline))
        candidate_receipt_path, candidate_receipt_sha = self._write(
            "candidate-receipt.json", candidate_receipt or receipt(self.candidate))
        inputs = self._inputs({"baseline": baseline_receipt_sha,
                               "candidate": candidate_receipt_sha})
        return inputs, baseline_receipt_path, candidate_receipt_path

    def _evaluate_verified(self, baseline_receipt=None, candidate_receipt=None):
        inputs, baseline_receipt_path, candidate_receipt_path = self._evidence_inputs(
            baseline_receipt, candidate_receipt)
        return evaluator.evaluate(*inputs, self.task_content_path,
                                  baseline_receipt_path, candidate_receipt_path)

    def _evaluate(self):
        return evaluator.evaluate(*self._inputs())

    def _rejects(self, code):
        with self.assertRaises(evaluator.EvaluationError) as raised:
            self._evaluate()
        self.assertEqual(raised.exception.code, code)

    def test_sha_bound_observations_remain_evidence_incomplete(self):
        result = self._evaluate()
        self.assertEqual(result["pair_status"], "EVIDENCE_INCOMPLETE")
        self.assertEqual(result["observation_pair_status"], "SHA_BOUND")
        self.assertIsNone(result["quality_gate"]["passed"])
        self.assertEqual(result["quality_gate"]["status"], "UNVERIFIED")
        self.assertEqual(result["quality_gate"]["reported_baseline_status"], "PASS")
        self.assertEqual(result["quality_gate"]["reported_candidate_status"], "PASS")
        self.assertEqual(result["measurements"]["baseline"]["elapsed_seconds"], 600)
        self.assertEqual(result["measurements"]["candidate"]["elapsed_seconds"], 540)
        self.assertFalse(result["comparability"]["elapsed_efficiency"])
        self.assertFalse(result["comparability"]["total_tokens"])
        self.assertIsNone(result["result"]["elapsed_direction"])
        self.assertIsNone(result["result"]["elapsed_delta_seconds"])
        self.assertIsNone(result["result"]["total_tokens_delta"])
        self.assertEqual(result["result"]["causal_gain"], "NOT_ESTABLISHED")
        self.assertFalse(result["evidence_scope"]["verifier_receipts_checked"])
        self.assertFalse(result["evidence_scope"]["task_content_bytes_checked"])
        self.assertEqual(result["quality_gate"]["basis"],
                         "SHA-bound observations; task content or verifier receipts not checked")

    def test_pinned_content_and_receipts_do_not_promote_reported_pass(self):
        result = self._evaluate_verified()
        self.assertEqual(result["pair_status"], "EVIDENCE_INCOMPLETE")
        self.assertEqual(result["observation_pair_status"], "SHA_BOUND_PARENT_RECEIPTS")
        self.assertIsNone(result["quality_gate"]["passed"])
        self.assertEqual(result["quality_gate"]["status"], "UNVERIFIED")
        self.assertTrue(result["quality_gate"]["receipt_reported_passed"])
        self.assertEqual(result["quality_gate"]["receipt_reported_status"], "PASS")
        self.assertIn("output bytes unverified", result["quality_gate"]["basis"])
        self.assertTrue(result["evidence_scope"]["task_content_bytes_checked"])
        self.assertTrue(result["evidence_scope"]["verifier_receipts_checked"])
        self.assertFalse(result["evidence_scope"]["verifier_authorship_authenticated"])
        self.assertFalse(result["evidence_scope"]["output_bytes_checked"])
        self.assertFalse(result["evidence_scope"]["provider_or_runtime_observed"])
        self.assertEqual(result["measurements"]["baseline"]["elapsed_source"],
                         "CALCULATED_FROM_REPORTED_TIMESTAMPS")
        self.assertEqual(result["measurements"]["candidate"]["usage_source"],
                         "REPORTED_OBSERVATION")
        self.assertFalse(result["comparability"]["elapsed_efficiency"])
        self.assertFalse(result["comparability"]["total_tokens"])
        self.assertIsNone(result["result"]["elapsed_delta_seconds"])
        self.assertIsNone(result["result"]["total_tokens_delta"])
        self.assertEqual(result["result"]["causal_gain"], "NOT_ESTABLISHED")

    def test_pinned_failing_receipt_reports_failure_without_verifying_gate(self):
        self.candidate["verifier"]["status"] = "FAIL"
        result = self._evaluate_verified()
        self.assertIsNone(result["quality_gate"]["passed"])
        self.assertEqual(result["quality_gate"]["status"], "UNVERIFIED")
        self.assertFalse(result["quality_gate"]["receipt_reported_passed"])
        self.assertEqual(result["quality_gate"]["receipt_reported_status"], "FAIL")
        self.assertEqual(result["pair_status"], "EVIDENCE_INCOMPLETE")

    def test_partial_optional_evidence_remains_incomplete(self):
        inputs, baseline_receipt_path, candidate_receipt_path = self._evidence_inputs()
        for task_content, baseline_receipt, candidate_receipt in (
            (None, baseline_receipt_path, candidate_receipt_path),
            (self.task_content_path, baseline_receipt_path, None),
            (self.task_content_path, None, candidate_receipt_path),
        ):
            with self.subTest(task_content=task_content, baseline=baseline_receipt,
                              candidate=candidate_receipt):
                result = evaluator.evaluate(*inputs, task_content, baseline_receipt,
                                            candidate_receipt)
                self.assertEqual(result["pair_status"], "EVIDENCE_INCOMPLETE")
                self.assertIsNone(result["quality_gate"]["passed"])
                self.assertEqual(result["quality_gate"]["status"], "UNVERIFIED")

    def test_unpinned_receipt_path_cannot_promote_quality(self):
        baseline_receipt_path, _ = self._write("baseline-receipt.json", receipt(self.baseline))
        result = evaluator.evaluate(*self._inputs(), self.task_content_path,
                                    baseline_receipt_path)
        self.assertIsNone(result["quality_gate"]["passed"])
        self.assertFalse(result["evidence_scope"]["verifier_receipts_checked"])

    def test_missing_output_identity_remains_incomplete(self):
        candidate_receipt = receipt(self.candidate)
        del self.candidate["output_sha256"]
        inputs, baseline_receipt_path, candidate_receipt_path = self._evidence_inputs(
            candidate_receipt=candidate_receipt)
        result = evaluator.evaluate(*inputs, self.task_content_path,
                                    baseline_receipt_path, candidate_receipt_path)
        self.assertEqual(result["pair_status"], "EVIDENCE_INCOMPLETE")
        self.assertIsNone(result["quality_gate"]["passed"])
        self.assertFalse(result["evidence_scope"]["verifier_receipts_checked"])

    def test_task_content_or_receipt_digest_mismatch_is_rejected(self):
        inputs, baseline_receipt_path, candidate_receipt_path = self._evidence_inputs()
        self.task_content_path.write_bytes(b"changed task")
        with self.assertRaises(evaluator.EvaluationError) as raised:
            evaluator.evaluate(*inputs, self.task_content_path,
                               baseline_receipt_path, candidate_receipt_path)
        self.assertEqual(raised.exception.code, "SHA256_MISMATCH")
        self.task_content_path.write_bytes(TASK_CONTENT)
        candidate_receipt_path.write_bytes(candidate_receipt_path.read_bytes() + b" ")
        with self.assertRaises(evaluator.EvaluationError) as raised:
            evaluator.evaluate(*inputs, self.task_content_path,
                               baseline_receipt_path, candidate_receipt_path)
        self.assertEqual(raised.exception.code, "SHA256_MISMATCH")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_pinned_input_rejects_fifo_without_waiting_for_a_writer(self):
        inputs, baseline_receipt_path, candidate_receipt_path = self._evidence_inputs()
        self.task_content_path.unlink()
        os.mkfifo(self.task_content_path)
        with self.assertRaises(evaluator.EvaluationError) as raised:
            evaluator.evaluate(*inputs, self.task_content_path,
                               baseline_receipt_path, candidate_receipt_path)
        self.assertEqual(raised.exception.code, "INVALID_INPUT")

    def test_receipt_must_match_exact_arm_run_task_output_and_verifier(self):
        for field, replacement, code in (
            ("arm", "baseline", "IDENTITY_MISMATCH"),
            ("run_id", "run-other", "IDENTITY_MISMATCH"),
            ("task_id", "task-other", "IDENTITY_MISMATCH"),
            ("task_sha256", hashlib.sha256(b"other task").hexdigest(), "IDENTITY_MISMATCH"),
            ("output_sha256", hashlib.sha256(b"other output").hexdigest(), "IDENTITY_MISMATCH"),
            ("executor_id", "other-executor", "IDENTITY_MISMATCH"),
            ("verifier_id", "other-reviewer", "INVALID_VERIFIER"),
            ("status", "FAIL", "INVALID_VERIFIER"),
        ):
            with self.subTest(field=field):
                candidate_receipt = receipt(self.candidate)
                candidate_receipt[field] = replacement
                with self.assertRaises(evaluator.EvaluationError) as raised:
                    self._evaluate_verified(candidate_receipt=candidate_receipt)
                self.assertEqual(raised.exception.code, code)

    def test_cross_arm_executor_cannot_verify_other_arm(self):
        self.candidate["verifier"]["verifier_id"] = self.baseline["executor_id"]
        with self.assertRaises(evaluator.EvaluationError) as raised:
            self._evaluate_verified()
        self.assertEqual(raised.exception.code, "INVALID_VERIFIER")

    def test_receipt_duplicate_keys_and_overflow_numbers_are_rejected(self):
        valid = json.dumps(receipt(self.baseline), sort_keys=True,
                           separators=(",", ":")).encode()
        invalid_receipts = (
            valid.replace(b'"arm":"baseline"', b'"arm":"baseline","arm":"baseline"'),
            valid[:-1] + b',"unexpected":1e999}',
            valid[:-1] + b',"unexpected":NaN}',
        )
        candidate_receipt_path, candidate_receipt_sha = self._write(
            "candidate-receipt.json", receipt(self.candidate))
        for raw in invalid_receipts:
            with self.subTest(raw=raw[-24:]):
                baseline_receipt_path = self.root / "baseline-receipt.json"
                baseline_receipt_path.write_bytes(raw)
                inputs = self._inputs({"baseline": hashlib.sha256(raw).hexdigest(),
                                       "candidate": candidate_receipt_sha})
                with self.assertRaises(evaluator.EvaluationError) as raised:
                    evaluator.evaluate(*inputs, self.task_content_path,
                                       baseline_receipt_path, candidate_receipt_path)
                self.assertEqual(raised.exception.code, "INVALID_JSON")

    def test_historical_like_mixed_task_pair_is_rejected_even_when_sha_bound(self):
        self.candidate["task_sha256"] = hashlib.sha256(b"older task revision").hexdigest()
        self._rejects("IDENTITY_MISMATCH")

    def test_task_model_and_effort_mismatches_are_rejected(self):
        for field, value in (("task_id", "other-task"), ("model", "other-model"),
                             ("effort", "medium")):
            with self.subTest(field=field):
                original = self.candidate[field]
                self.candidate[field] = value
                self._rejects("IDENTITY_MISMATCH")
                self.candidate[field] = original

    def test_zero_usage_is_not_missing_usage(self):
        self.baseline["usage"] = {"status": "PARTIAL", "total_tokens": 0}
        self.candidate["usage"] = {"status": "UNKNOWN"}
        result = self._evaluate()
        self.assertEqual(result["measurements"]["baseline"]["usage"]["total_tokens"], 0)
        self.assertNotIn("total_tokens", result["measurements"]["candidate"]["usage"])
        self.assertFalse(result["comparability"]["total_tokens"])
        self.assertIsNone(result["result"]["total_tokens_delta"])

    def test_partial_usage_retains_observed_total_without_inference(self):
        self.baseline["usage"] = {"status": "PARTIAL", "total_tokens": 11}
        self.candidate["usage"] = {"status": "PARTIAL", "total_tokens": 9}
        result = self._evaluate()
        self.assertEqual(result["measurements"]["baseline"]["usage"],
                         {"status": "PARTIAL", "total_tokens": 11})
        self.assertFalse(result["comparability"]["total_tokens"])
        self.assertIsNone(result["result"]["total_tokens_delta"])

    def test_cli_aggregate_label_and_cached_tokens_are_preserved_not_upgraded(self):
        self.baseline["usage"].update({"provenance": "CLI_AGGREGATE",
                                        "cached_input_tokens": 17})
        self.candidate["usage"] = {"status": "PARTIAL", "cached_input_tokens": 0,
                                   "provenance": "CLI_AGGREGATE"}
        result = self._evaluate_verified()
        self.assertEqual(result["measurements"]["baseline"]["usage"]["cached_input_tokens"], 17)
        self.assertEqual(result["measurements"]["candidate"]["usage"],
                         {"status": "PARTIAL", "cached_input_tokens": 0,
                          "provenance": "CLI_AGGREGATE"})
        self.assertEqual(result["measurements"]["baseline"]["provider_usage_provenance"],
                         "UNKNOWN")
        self.assertEqual(result["measurements"]["candidate"]["usage_source"],
                         "REPORTED_OBSERVATION")
        self.assertIsNone(result["result"]["total_tokens_delta"])

    def test_invalid_cached_usage_is_rejected(self):
        self.baseline["usage"]["cached_input_tokens"] = -1
        self._rejects("INVALID_USAGE")
        self.baseline["usage"]["cached_input_tokens"] = 0
        self.baseline["usage"]["provenance"] = "PROVIDER_VERIFIED"
        self._rejects("INVALID_USAGE")

    def test_reported_quality_failure_does_not_claim_verified_gate(self):
        self.candidate["verifier"]["status"] = "FAIL"
        result = self._evaluate()
        self.assertEqual(result["pair_status"], "EVIDENCE_INCOMPLETE")
        self.assertIsNone(result["quality_gate"]["passed"])
        self.assertEqual(result["quality_gate"]["reported_candidate_status"], "FAIL")
        self.assertEqual(result["result"]["status"], "EVIDENCE_INCOMPLETE")
        self.assertIsNone(result["result"]["elapsed_direction"])
        self.assertIsNone(result["result"]["elapsed_delta_seconds"])
        self.assertIsNone(result["result"]["total_tokens_delta"])
        self.assertEqual(result["measurements"]["candidate"]["elapsed_seconds"], 540)

    def test_incomplete_or_unauthorized_arms_are_rejected(self):
        self.candidate["run_status"] = "PARTIAL"
        self._rejects("INCOMPLETE_ARM")
        self.candidate["run_status"] = "COMPLETE"
        self.candidate["arm"] = "historical"
        self._rejects("UNAUTHORIZED_ARM")

    def test_self_verification_and_unknown_verifier_status_are_rejected(self):
        self.candidate["verifier"]["verifier_id"] = self.candidate["executor_id"]
        self._rejects("INVALID_VERIFIER")
        self.candidate["verifier"]["verifier_id"] = "reviewer-1"
        self.candidate["verifier"]["status"] = "UNKNOWN"
        self._rejects("INVALID_VERIFIER")

    def test_naive_or_reversed_timestamps_are_rejected(self):
        self.candidate["started_at"] = "2026-09-01T11:00:00"
        self._rejects("INVALID_TIMESTAMP")
        self.candidate["started_at"] = "2026-09-01T11:12:00+00:00"
        self._rejects("INVALID_TIMESTAMP")

    def test_observation_hash_change_is_rejected(self):
        task_path, task_sha, baseline_path, candidate_path = self._inputs()
        candidate_path.write_bytes(candidate_path.read_bytes() + b" ")
        with self.assertRaises(evaluator.EvaluationError) as raised:
            evaluator.evaluate(task_path, task_sha, baseline_path, candidate_path)
        self.assertEqual(raised.exception.code, "SHA256_MISMATCH")

    def test_duplicate_json_keys_are_rejected_after_hash_check(self):
        task_path, task_sha, baseline_path, candidate_path = self._inputs()
        duplicate = (b'{"schema_version":"paired-long-task-observation/v1",'
                     b'"arm":"candidate","arm":"candidate"}')
        candidate_path.write_bytes(duplicate)
        task = json.loads(task_path.read_text())
        task["arms"]["candidate"] = hashlib.sha256(duplicate).hexdigest()
        task_path, task_sha = self._write("task.json", task)
        with self.assertRaises(evaluator.EvaluationError) as raised:
            evaluator.evaluate(task_path, task_sha, baseline_path, candidate_path)
        self.assertEqual(raised.exception.code, "INVALID_JSON")

    def test_cli_emits_typed_rejection_without_partial_result(self):
        self.candidate["run_status"] = "PARTIAL"
        task_path, task_sha, baseline_path, candidate_path = self._inputs()
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--frozen-task", str(task_path),
             "--frozen-task-sha256", task_sha, "--baseline", str(baseline_path),
             "--candidate", str(candidate_path)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["pair_status"], "REJECTED")
        self.assertEqual(result["error"]["code"], "INCOMPLETE_ARM")
        self.assertNotIn("measurements", result)

    def test_cli_never_promotes_self_reported_verification(self):
        task_path, task_sha, baseline_path, candidate_path = self._inputs()
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--frozen-task", str(task_path),
             "--frozen-task-sha256", task_sha, "--baseline", str(baseline_path),
             "--candidate", str(candidate_path)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["pair_status"], "EVIDENCE_INCOMPLETE")
        self.assertIsNone(result["quality_gate"]["passed"])
        self.assertFalse(result["comparability"]["elapsed_efficiency"])

    def test_cli_reads_all_optional_evidence_without_efficiency_claim(self):
        inputs, baseline_receipt_path, candidate_receipt_path = self._evidence_inputs()
        task_path, task_sha, baseline_path, candidate_path = inputs
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--frozen-task", str(task_path),
             "--frozen-task-sha256", task_sha, "--baseline", str(baseline_path),
             "--candidate", str(candidate_path), "--task-content", str(self.task_content_path),
             "--baseline-verifier-receipt", str(baseline_receipt_path),
             "--candidate-verifier-receipt", str(candidate_receipt_path)],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["quality_gate"]["status"], "UNVERIFIED")
        self.assertTrue(result["quality_gate"]["receipt_reported_passed"])
        self.assertIsNone(result["quality_gate"]["passed"])
        self.assertEqual(result["pair_status"], "EVIDENCE_INCOMPLETE")
        self.assertFalse(result["comparability"]["elapsed_efficiency"])


if __name__ == "__main__":
    unittest.main()
