import copy
import unittest

from codex_collaboration_harness.batch import _verifier_delivery
from codex_collaboration_harness import batch
from test_summary_compaction import fixture


class VerifierDeliveryTests(unittest.TestCase):
    def evidence(self):
        return {
            "status": "EVIDENCE_VERIFIED", "artifact_integrity": "verified",
            "commands": [{"event_index": 18, "execution_proof": "known_success",
                          "receipt_check": "matched", "exit_code": 0}],
            "pagination": {"next_offset": 19},
            "parent_review_context": {"verifier_contexts": [{
                "source_event": {"event_index": 18, "verifier_index": 0,
                                 "receipt_sha256": "a" * 64},
                "source_postimages": {"files": [{"path": "source.py",
                    "postimage_sha256": "b" * 64, "spans": [{"text": "not repeated"}]}]},
            }]},
        }

    def test_projects_identity_without_source_or_acceptance(self):
        evidence = self.evidence()
        original = copy.deepcopy(evidence)
        result = _verifier_delivery(evidence)
        self.assertTrue(result["parent_acceptance_required"])
        self.assertFalse(result["live_source_freshness"])
        self.assertFalse(result["dependency_coverage"])
        self.assertEqual(result["pagination"], {"next_offset": 19})
        self.assertEqual(result["receipts"][0]["source_event"]["receipt_sha256"], "a" * 64)
        self.assertNotIn("spans", result["receipts"][0]["source_postimages"][0])
        self.assertEqual(evidence, original)

    def test_does_not_promote_failed_unknown_or_unmatched_commands(self):
        for key, value in (("event_index", 17), ("event_index", True),
                           ("exit_code", 1), ("exit_code", False),
                           ("execution_proof", "unknown"), ("receipt_check", "mismatch")):
            with self.subTest(key=key, value=value):
                evidence = self.evidence()
                evidence["commands"][0][key] = value
                self.assertIsNone(_verifier_delivery(evidence))

    def test_missing_or_invalid_inspection_is_not_success(self):
        self.assertIsNone(_verifier_delivery({}))
        for key, value in (("status", "UNKNOWN"), ("artifact_integrity", "unknown"),
                           ("first_blocker", "INVALID"), ("parent_review_context", "bad")):
            evidence = self.evidence()
            evidence[key] = value
            self.assertIsNone(_verifier_delivery(evidence))

    def test_full_summary_delivers_receipt(self):
        result = fixture(1)
        result["members"][0]["terminal"]["result_inspection"].update(self.evidence())
        row = batch.expand_summary(batch.summarize(result))["members"][0]
        self.assertEqual(len(row["verifier_delivery"]["receipts"]), 1)
        self.assertEqual(row["result_text"], "bounded answer")

    def test_success_does_not_require_optional_source_context(self):
        evidence = self.evidence()
        evidence["parent_review_context"] = {}
        evidence["terminal_sha256"] = "c" * 64
        evidence["commands"][0]["command"] = '{"verifier_index":0}'
        result = _verifier_delivery(evidence)
        self.assertEqual(result["receipts"][0]["source_event"]["terminal_sha256"], "c" * 64)
        self.assertFalse(result["receipts"][0]["source_context_available"])
        for command in ('{"verifier_index":true}', '{"verifier_index":-1}',
                        '{"verifier_index":0,"other":1}', 'echo verifier_index'):
            evidence["commands"][0]["command"] = command
            self.assertIsNone(_verifier_delivery(evidence))

    def test_optional_projection_cannot_break_existing_result_budget(self):
        result = fixture(1)
        baseline = batch.summarize(copy.deepcopy(result))
        evidence = self.evidence()
        evidence["parent_review_context"]["verifier_contexts"][0]["source_event"]["receipt_path"] = "x" * 100000
        result["members"][0]["terminal"]["result_inspection"].update(evidence)
        actual = batch.expand_summary(batch.summarize(result))
        self.assertEqual(actual["status"], baseline["status"])
        self.assertEqual(actual["members"][0]["result_text"], "bounded answer")
        self.assertNotIn("verifier_delivery", actual["members"][0])


if __name__ == "__main__":
    unittest.main()
