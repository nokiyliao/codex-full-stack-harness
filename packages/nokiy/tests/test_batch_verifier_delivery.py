import copy
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_collaboration_harness.batch import _verifier_delivery
from codex_collaboration_harness import batch, postimage_context
from test_summary_compaction import fixture
import test_result_inspection as inspection_fixtures


def verified_evidence(text="def support():\n    return 2\n"):
    return {
        "status": "EVIDENCE_VERIFIED", "artifact_integrity": "verified",
        "file_changes": {"postimages": [{"path": "source.py", "sha256": "b" * 64}]},
        "commands": [{"event_index": 18, "execution_proof": "known_success",
                      "receipt_check": "matched", "exit_code": 0}],
        "pagination": {"next_offset": 19},
        "parent_review_context": {"verifier_contexts": [{
            "source_event": {"event_index": 18, "verifier_index": 0,
                             "receipt_sha256": "a" * 64},
            "source_postimages": {
                "schema_version": postimage_context.SCHEMA_VERSION,
                "kind": "verifier_postimage_context", "jspace_semantic_sha256": "c" * 64,
                "notice": postimage_context.NOTICE,
                "files": [{"path": "source.py", "preimage_sha256": "d" * 64,
                    "postimage_sha256": "b" * 64,
                    "locators": [{"qualified_name": "support", "kind": "function", "line": 1,
                                  "start_line": 1, "end_line": 2, "complete": True}],
                    "spans": [{"start_line": 1, "end_line": 2, "text": text}]}]},
        }]},
    }


def public_note(text="Semantic delta and retained invariants; source.py:1; parent review pending."):
    return {
        "schema_version": "nokiy_task_status_final_reply_v1", "status": "MODEL_REPORTED",
        "binding": {"request_id": "tura_embedded_" + "a" * 64, "request_sha256": "a" * 64,
                    "native_thread_id": "fixture-thread", "runtime_id": "fixture-runtime"},
        "source_artifact": {"path": "/fixture/core.jsonl", "sha256": "b" * 64, "bytes": 100},
        "output_json_pointer": "/task_status/final_reply", "text": text,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(), "content_bytes": len(text.encode()),
        "complete": True, "truncated": False, "truncation_reason": None,
        "parent_acceptance_required": True,
    }


class PublicHandoffDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = inspection_fixtures.InspectionTests(
            "test_native_terminal_final_reply_readback_preserves_original_unavailable_handoff")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def summarize(self, projected):
        result = fixture(1)
        result["members"][0].update(request_id=projected["request_id"], artifact_root=str(self.f.root),
                                     terminal=projected)
        with patch.object(batch, "project_terminal", side_effect=AssertionError("second inspection")), \
             patch.object(batch.caller, "execute", side_effect=AssertionError("model execution")), \
             patch.object(Path, "open", side_effect=AssertionError("artifact reread")):
            return batch.expand_summary(batch.summarize(result))["members"][0]

    def test_same_turn_public_note_stays_separate_model_reported_data(self):
        terminal, _, events = self.f.native_terminal_reply_fixture(
            "Semantic delta: cache immutable headers only. Retained invariants: current fold checks. "
            "Evidence: source.py:1. Parent acceptance pending.")
        projected = self.f.project_capability(terminal)
        before = copy.deepcopy(projected)
        row = self.summarize(projected)
        self.assertEqual(row["task_status_final_reply"], projected["task_status_final_reply"])
        self.assertEqual(row["task_status_final_reply"]["status"], "MODEL_REPORTED")
        self.assertTrue(row["task_status_final_reply"]["parent_acceptance_required"])
        self.assertEqual(row["result_text"], terminal["result_text"])
        self.assertNotIn("result_text_source", row)
        self.assertNotIn("parent_review_answer", projected["result_inspection"])
        self.assertEqual(row["evidence_status"], projected["result_inspection"]["status"])
        self.assertFalse(row["terminal_evidence"]["final_summary_turn_executed"])
        self.assertEqual(events[1]["item"]["runtime_id"], events[2]["runtime_id"])
        self.assertEqual(projected, before)

    def test_absent_note_is_not_invented_and_raw_terminal_note_is_not_trusted(self):
        terminal, _, _ = self.f.terminal_evidence_fixture()
        terminal["task_status_final_reply"] = public_note("Untrusted raw terminal claim")
        projected = self.f.project_capability(terminal)
        self.assertNotIn("task_status_final_reply", projected)
        self.assertNotIn("task_status_final_reply", self.summarize(projected))

    def test_wrong_binding_or_malformed_public_note_uses_existing_rejection(self):
        for invalid in ("request", "command"):
            terminal, run, events = self.f.native_terminal_reply_fixture("public semantic delta")
            if invalid == "request":
                events[1]["item"]["request_id"] = "tura_embedded_" + "f" * 64
            else:
                events[1]["item"]["command"] = '{"status":"done","final_reply":"different"}'
            self.f.republish_terminal_evidence(terminal, run, events)
            with self.subTest(invalid=invalid):
                projected = self.f.project_capability(terminal)
                self.assertNotIn("task_status_final_reply", projected)
                self.assertNotIn("task_status_final_reply", self.summarize(projected))

    def test_existing_note_truncation_and_binding_are_preserved(self):
        terminal, _, _ = self.f.native_terminal_reply_fixture("public delta " * 100, max_result_bytes=256)
        projected = self.f.project_capability(terminal)
        row = self.summarize(projected)
        self.assertEqual(row["task_status_final_reply"], projected["task_status_final_reply"])
        self.assertFalse(row["task_status_final_reply"]["complete"])
        self.assertTrue(row["task_status_final_reply"]["truncated"])
        self.assertEqual(row["task_status_final_reply"]["truncation_reason"], "MAX_RESULT_BYTES")


class VerifierDeliveryTests(unittest.TestCase):
    def evidence(self):
        return verified_evidence()

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

    def test_verified_support_is_delivered_verbatim_without_io_or_reinspection(self):
        evidence = self.evidence()
        before = copy.deepcopy(evidence)
        source = evidence["parent_review_context"]["verifier_contexts"][0]["source_postimages"]
        with patch.object(Path, "open", side_effect=AssertionError("source reread")), \
             patch.object(batch, "project_terminal", side_effect=AssertionError("reinspection")), \
             patch.object(batch.caller, "execute", side_effect=AssertionError("execution")):
            delivery = _verifier_delivery(evidence)
        self.assertEqual(delivery["receipts"][0]["source_postimage_context"], source)
        self.assertTrue(delivery["parent_acceptance_required"])
        self.assertEqual(evidence, before)

    def test_partial_stale_or_unrecognized_context_does_not_displace_receipts(self):
        for mutation in ("partial", "stale", "incomplete_definition", "unknown_schema"):
            with self.subTest(mutation=mutation):
                evidence = self.evidence()
                source = evidence["parent_review_context"]["verifier_contexts"][0]["source_postimages"]
                if mutation == "partial":
                    evidence["projection_partial"] = True
                elif mutation == "stale":
                    source["files"][0]["postimage_sha256"] = "e" * 64
                elif mutation == "incomplete_definition":
                    source["files"][0]["locators"][0]["complete"] = False
                else:
                    source["schema_version"] = "unknown"
                receipts = _verifier_delivery(evidence)["receipts"]
                self.assertEqual(len(receipts), 1)
                self.assertNotIn("source_postimage_context", receipts[0])
                self.assertEqual(receipts[0]["source_event"]["receipt_sha256"], "a" * 64)

    def test_identical_context_is_deduplicated_but_unique_support_is_retained(self):
        evidence = self.evidence()
        contexts = evidence["parent_review_context"]["verifier_contexts"]
        duplicate = copy.deepcopy(contexts[0])
        duplicate["source_event"]["event_index"] = 19
        unique = copy.deepcopy(contexts[0])
        unique["source_event"]["event_index"] = 20
        unique["source_postimages"]["files"][0]["path"] = "other.py"
        unique["source_postimages"]["files"][0]["spans"][0]["text"] = "def support():\n    return 3\n"
        contexts.extend((duplicate, unique))
        for index in (19, 20):
            evidence["commands"].append({**evidence["commands"][0], "event_index": index})
        evidence["file_changes"]["postimages"].append({"path": "other.py", "sha256": "b" * 64})
        receipts = _verifier_delivery(evidence)["receipts"]
        self.assertEqual(len(receipts), 3)
        self.assertIn("source_postimage_context", receipts[0])
        self.assertNotIn("source_postimage_context", receipts[1])
        self.assertEqual(receipts[2]["source_postimage_context"], unique["source_postimages"])

    def test_delta_context_preserves_schema_coverage_and_navigation(self):
        for schema in (postimage_context.DELTA_SCHEMA_VERSION, postimage_context.DELTA_NAVIGATION_SCHEMA_VERSION):
            with self.subTest(schema=schema):
                evidence = self.evidence()
                source = evidence["parent_review_context"]["verifier_contexts"][0]["source_postimages"]
                source.update(schema_version=schema, kind="verifier_postimage_delta", notice=(
                    postimage_context.DELTA_NOTICE if schema == postimage_context.DELTA_SCHEMA_VERSION
                    else postimage_context.DELTA_NAVIGATION_NOTICE))
                file = source["files"][0]
                file.pop("spans")
                if schema == postimage_context.DELTA_SCHEMA_VERSION:
                    file.pop("locators")
                else:
                    file["locators"][0].pop("complete")
                file.update(coverage="all_text_changes", hunks=[{
                    "before_start_line": 1, "before_line_count": 1, "before_text": "old\n",
                    "after_start_line": 1, "after_line_count": 1, "after_text": "new\n"}])
                self.assertEqual(_verifier_delivery(evidence)["receipts"][0]["source_postimage_context"], source)
                file["coverage"] = "partial"
                self.assertNotIn("source_postimage_context", _verifier_delivery(evidence)["receipts"][0])

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
