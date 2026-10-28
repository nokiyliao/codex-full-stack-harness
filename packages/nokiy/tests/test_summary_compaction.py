"""Offline summary contract: metadata compaction never grants result acceptance."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from codex_collaboration_harness import batch
from codex_collaboration_harness.embedded_nokiy import EmbeddedNokiyError
from codex_collaboration_harness.result_inspection import MAX_PROJECTED_BYTES


def fixture(count=5):
    rows = []
    for i in range(count):
        root = "/Volumes/NOKIY-TB5/UTM/runtime_repros/" + "bounded-long-task-" * 5 + str(i)
        rid = "tura_embedded_" + f"{i + 1:064x}"
        terminal = {
            "model": f"model-{i}", "reasoning_effort": "max",
            "model_route": {"configured_provider": "official_codex_app_server",
                            "requested_model": "gpt-6.1-sol", "not_provider_observation": True},
            "requested_service_tier": "default", "observed_service_tier": None,
            "usage": {"input_tokens": 1000 + i, "cached_input_tokens": 200 + i,
                      "output_tokens": 10 + i, "total_tokens": 1010 + 2 * i,
                      "coverage": {"schema_version": "runtime_usage_coverage_v1",
                          "scope": "recorded_session_context_runtimes_not_provider_attempts_or_billing",
                          "status": "complete", "known_runtime_count": 10,
                          "valid_usage_count": 10, "missing_usage_count": 0}},
            "result_text": "bounded answer",
            "result_inspection": {
                "status": "EVIDENCE_VERIFIED" if i != 4 else "INCOMPLETE_EVIDENCE",
                "counts": {"events": 41 + i, "commands": 30 + i, "indexed": 30 + i,
                           "file_changes": i, "failed": i == 4, "index_complete": True},
                "first_blocker": None if i != 4 else "COMMAND_FAILURE",
                "terminal_sha256": f"{i + 30:064x}",
                "source_read_efficiency": {"coverage": "complete", "source_read_calls": 20 + i,
                    "returned_lines": 600 + i, "unique_lines": 590 + i, "repeated_lines": 10,
                    "source_bytes_excluding_newlines": 4000, "skipped_unproven": 0,
                    "command_run_batches": 4, "conflicting_lines": 0, "index_complete": True}},
        }
        rows.append({"request_id": rid, "artifact_root": root,
                     "status": "RESULT_AVAILABLE" if i != 4 else "BLOCKED",
                     "first_typed_blocker": None if i != 4 else "COMMAND_FAILURE",
                     "cleanup_pass": i != 4, "terminal": terminal})
    return {"status": "BLOCKED" if count == 5 else "RESULT_AVAILABLE",
            "first_typed_blocker": "COMMAND_FAILURE" if count == 5 else None,
            "members": rows}


class SummaryCompactionTests(unittest.TestCase):
    def encoded(self, value):
        return json.dumps(value, ensure_ascii=True, sort_keys=True).encode()

    def review_fixture(self, count=2):
        result = fixture(count)
        answers = []
        for index, row in enumerate(result["members"]):
            text = json.dumps({"findings": [
                {"id": question, "answer": f"source-{index}-{question}: " + "checked evidence " * 55}
                for question in range(12)
            ]})
            answers.append(text)
            terminal = row["terminal"]
            terminal.update(result_text=text[:600], result_truncated=True, result_artifact={
                "path": f"/answers/{index}.txt", "bytes": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest()})
            terminal["result_inspection"]["parent_review_answer"] = {
                "schema_version": "nokiy_parent_review_answer_v1", "text": text,
                "artifact": terminal["result_artifact"], "parent_acceptance_required": True,
                "terminal_content_sha256": f"{index + 40:064x}",
            }
        return result, answers

    def test_verified_batch_answers_need_no_followup_reads(self):
        result, answers = self.review_fixture()
        before = copy.deepcopy(result)
        with patch.object(batch.caller, "execute", side_effect=AssertionError("model execution")), \
             patch.object(batch, "project_terminal", side_effect=AssertionError("second inspection")), \
             patch.object(batch.caller, "read_terminal", side_effect=AssertionError("second read")), \
             patch.object(batch.Path, "open", side_effect=AssertionError("file I/O")):
            summary = batch.summarize(result)
        self.assertLessEqual(len(self.encoded(summary)), MAX_PROJECTED_BYTES)
        for row, text, original in zip(batch.expand_summary(summary)["members"], answers, before["members"]):
            self.assertEqual(row["result_text"], text)
            self.assertFalse(row["result_truncated"])
            self.assertEqual(row["result_text_source"], "verified_final_artifact")
            self.assertTrue(row["terminal_result_truncated"])
            self.assertEqual(json.loads(row["result_text"])["findings"][-1]["id"], 11)
            self.assertEqual(row["result_artifact"], original["terminal"]["result_artifact"])
            self.assertEqual(row["terminal_sha256"], original["terminal"]["result_inspection"]["terminal_sha256"])
            self.assertEqual(row["usage"], original["terminal"]["usage"])
            self.assertNotIn("parent_review_answer", row)
        self.assertEqual(summary["mission_acceptance"], "parent_owned")
        self.assertEqual(result, before)

    def test_incomplete_inspection_never_promotes_an_answer(self):
        result, _ = self.review_fixture(1)
        terminal = result["members"][0]["terminal"]
        terminal["result_inspection"].update(status="INCOMPLETE_EVIDENCE", first_blocker="COMMAND_FAILURE")
        result.update(status="BLOCKED", first_typed_blocker="COMMAND_FAILURE")
        result["members"][0].update(status="BLOCKED", first_typed_blocker="COMMAND_FAILURE")
        summary = batch.summarize(result)
        row = batch.expand_summary(summary)["members"][0]
        self.assertEqual(row["result_text"], terminal["result_text"])
        self.assertTrue(row["result_truncated"])
        self.assertNotIn("result_text_source", row)
        self.assertEqual(summary["first_typed_blocker"], "COMMAND_FAILURE")

    def test_resolved_verifier_failure_survives_without_reinspection(self):
        result = fixture(1)
        inspection = result["members"][0]["terminal"]["result_inspection"]
        inspection["counts"]["failed"] = 1
        inspection["command_success"] = "known_failure"
        inspection["verifier_failure_resolution"] = {
            "schema_version": "nokiy_focused_verifier_resolution_v1",
            "resolved_count": 1, "unresolved_count": 0,
            "effective_command_success": "known_success",
        }
        before = copy.deepcopy(result)
        with patch.object(batch, "project_terminal", side_effect=AssertionError("second inspection")), \
             patch.object(batch.caller, "read_terminal", side_effect=AssertionError("second read")), \
             patch.object(batch.Path, "open", side_effect=AssertionError("file I/O")):
            row = batch.expand_summary(batch.summarize(result))["members"][0]
        self.assertEqual(row["verifier_failure_resolution"], inspection["verifier_failure_resolution"])
        self.assertEqual(row["evidence_counts"]["failed"], 1)
        self.assertEqual(row["evidence_status"], "EVIDENCE_VERIFIED")
        self.assertEqual(result, before)

    def test_unresolved_verifier_failure_is_not_promoted_by_projection(self):
        result = fixture(5)
        terminal = result["members"][4]["terminal"]
        terminal["result_inspection"]["verifier_failure_resolution"] = {
            "schema_version": "nokiy_focused_verifier_resolution_v1",
            "resolved_count": 1, "unresolved_count": 1,
            "effective_command_success": "known_failure",
        }
        row = batch.expand_summary(batch.summarize(result))["members"][4]
        self.assertEqual(row["verifier_failure_resolution"]["unresolved_count"], 1)
        self.assertEqual(row["evidence_status"], "INCOMPLETE_EVIDENCE")
        self.assertEqual(row["evidence_first_blocker"], "COMMAND_FAILURE")
        self.assertNotIn("delivery_binding", row)

    def test_absent_verifier_resolution_is_not_invented(self):
        row = batch.expand_summary(batch.summarize(fixture(1)))["members"][0]
        self.assertNotIn("verifier_failure_resolution", row)

    def test_batch_delivery_budget_still_marks_partial_answers(self):
        result, _ = self.review_fixture(6)
        summary = batch.summarize(result)
        self.assertLessEqual(len(self.encoded(summary)), MAX_PROJECTED_BYTES)
        rows = batch.expand_summary(summary)["members"]
        self.assertEqual(len(rows), 6)
        self.assertTrue(any(row["result_truncated"] for row in rows))
        for row, original in zip(rows, result["members"]):
            self.assertEqual(row["request_id"], original["request_id"])
            self.assertEqual(row["result_artifact"], original["terminal"]["result_artifact"])

    def test_five_members_keep_all_identity_proof_usage_and_failure(self):
        result = fixture()
        original = copy.deepcopy(result)
        summary = batch.summarize(result)
        self.assertEqual(summary["schema_version"], "nokiy_direct_batch_summary_v2")
        self.assertLessEqual(len(self.encoded(summary)), MAX_PROJECTED_BYTES)
        self.assertEqual(len(summary["members"]), 5)
        expanded = batch.expand_summary(summary)
        for actual, row in zip(expanded["members"], result["members"]):
            self.assertEqual(actual["request_id"], row["request_id"])
            self.assertEqual(actual["artifact_root"], row["artifact_root"])
            self.assertEqual(actual["terminal_path"],
                             row["artifact_root"] + "/" + row["request_id"] + "/terminal.json")
            self.assertEqual(actual["usage"], row["terminal"]["usage"])
            self.assertEqual(actual["model"], row["terminal"]["model"])
            self.assertEqual(actual["evidence_counts"], row["terminal"]["result_inspection"]["counts"])
            self.assertEqual(actual["first_typed_blocker"], row["first_typed_blocker"])
            self.assertEqual(actual["cleanup_pass"], row["cleanup_pass"])
        self.assertEqual(expanded["status"], "BLOCKED")
        self.assertEqual(expanded["first_typed_blocker"], "COMMAND_FAILURE")
        self.assertEqual(result, original)

    def test_small_summary_remains_v1_and_preserves_text(self):
        summary = batch.summarize(fixture(1))
        self.assertEqual(summary["schema_version"], "nokiy_direct_batch_summary_v1")
        self.assertEqual(summary["members"][0]["result_text"], "bounded answer")
        self.assertNotIn("member_defaults", summary)
        self.assertIs(batch.expand_summary(summary), summary)

    def test_metadata_roundtrip_is_lossless_and_never_inspects_or_executes(self):
        with patch.object(batch.caller, "execute", side_effect=AssertionError("model execution")), \
             patch.object(batch, "project_terminal", side_effect=AssertionError("new inspection")):
            # The private transform can also handle v1 summaries below the threshold.
            summary = batch.summarize(fixture(1))
            compact = batch._compact_summary(summary)
            self.assertEqual(batch.expand_summary(compact), summary)

    def test_handoff_fields_survive_v1_v2_expansion_and_trimming_without_io(self):
        fields = ("observed_model", "wall_time_seconds", "result_artifact")
        cases = ((1, False), (5, False), (5, True))
        with patch.object(batch.caller, "execute", side_effect=AssertionError("model execution")), \
             patch.object(batch, "project_terminal", side_effect=AssertionError("new inspection")), \
             patch.object(batch.caller, "read_terminal", side_effect=AssertionError("terminal read")), \
             patch.object(batch, "retain_inspection_summary", side_effect=AssertionError("publication")), \
             patch("builtins.open", side_effect=AssertionError("file I/O")), \
             patch.object(batch.Path, "open", side_effect=AssertionError("path I/O")):
            for count, trim in cases:
                with self.subTest(count=count, trim=trim):
                    result = fixture(count)
                    for i, member in enumerate(result["members"]):
                        terminal = member["terminal"]
                        terminal["result_text"] = "\U0001f680" * 20000 if trim else "bounded answer"
                        terminal.update({
                            "observed_model": f"provider-model-{i}",
                            "wall_time_seconds": i * 1.25,
                            "result_artifact": {
                                "path": f"/answers/{i}.txt", "sha256": f"{i + 90:064x}",
                                "bytes": len(terminal["result_text"].encode("utf-8"))},
                        })
                    original = copy.deepcopy(result)
                    summary = batch.summarize(result)
                    self.assertEqual(summary["schema_version"],
                                     f"nokiy_direct_batch_summary_v{1 if count == 1 else 2}")
                    summary_before = copy.deepcopy(summary)
                    compact = (batch._compact_summary(summary) if count == 1 else summary)
                    compact_before = copy.deepcopy(compact)
                    expanded = batch.expand_summary(compact)
                    for projection in (summary, compact, expanded):
                        self.assertEqual(len(projection["members"]), count)
                        self.assertNotIn("wall_time_seconds", projection)
                        for actual, member in zip(projection["members"], original["members"]):
                            for key in fields:
                                self.assertEqual(actual[key], member["terminal"][key])
                    self.assertTrue(set(fields).isdisjoint(compact["member_defaults"]))
                    for projection in (summary, compact):
                        self.assertLessEqual(len(self.encoded(projection)), MAX_PROJECTED_BYTES)
                    if trim:
                        self.assertTrue(any(row["result_truncated"] for row in expanded["members"]))
                    else:
                        self.assertTrue(all(row["result_text"] == "bounded answer"
                                            for row in expanded["members"]))
                    self.assertEqual(summary, summary_before)
                    self.assertEqual(compact, compact_before)
                    self.assertEqual(result, original)

    def test_missing_and_unknown_handoff_fields_stay_unknown(self):
        fields = ("observed_model", "wall_time_seconds", "result_artifact")
        result = fixture(3)
        for key in fields:
            result["members"][0]["terminal"].pop(key, None)
        result["members"][1]["terminal"].update(dict.fromkeys(fields))
        result["members"][1]["terminal"]["usage"] = None
        result["members"][2]["terminal"] = None
        original = copy.deepcopy(result)
        summary = batch.summarize(result)
        self.assertEqual(summary["schema_version"], "nokiy_direct_batch_summary_v1")
        compact = batch._compact_summary(summary)
        expanded = batch.expand_summary(compact)
        for projection in (summary, compact, expanded):
            for row in projection["members"]:
                for key in fields:
                    self.assertIn(key, row)
                    self.assertIsNone(row[key])
            self.assertIsNone(projection["members"][1]["usage"])
            self.assertIsNone(projection["members"][2]["usage"])
            self.assertNotIn("wall_time_seconds", projection)
        for projection in (summary, compact):
            self.assertLessEqual(len(self.encoded(projection)), MAX_PROJECTED_BYTES)
        self.assertEqual(result, original)

    def test_different_routes_tiers_and_coverage_never_share(self):
        result = fixture()
        for i, row in enumerate(result["members"]):
            row["terminal"]["model_route"] = {"provider": str(i)}
            row["terminal"]["requested_service_tier"] = str(i)
            row["terminal"]["observed_service_tier"] = str(i)
            row["terminal"]["usage"]["coverage"]["known_runtime_count"] = i
        summary = batch.summarize(result)
        self.assertNotIn("model_route", summary["member_defaults"])
        self.assertNotIn("usage", summary["member_defaults"])
        expanded = batch.expand_summary(summary)
        for i, row in enumerate(expanded["members"]):
            self.assertEqual(row["usage"], result["members"][i]["terminal"]["usage"])
            self.assertEqual(row["model_route"], {"provider": str(i)})

    def test_bool_and_integer_are_not_coalesced_as_equal(self):
        result = fixture(2)
        result["members"][0]["terminal"]["model_route"] = {"value": True}
        result["members"][1]["terminal"]["model_route"] = {"value": 1}
        summary = batch.summarize(result)
        compact = batch._compact_summary(summary)
        self.assertNotIn("model_route", compact["member_defaults"])
        self.assertEqual(batch.expand_summary(compact), summary)

    def test_unicode_trim_is_bounded_and_marked_not_metadata_loss(self):
        result = fixture()
        for row in result["members"]:
            row["terminal"]["result_text"] = "\U0001f680" * 20000
        summary = batch.summarize(result)
        self.assertLessEqual(len(self.encoded(summary)), MAX_PROJECTED_BYTES)
        self.assertEqual(len(summary["members"]), 5)
        self.assertTrue(any(row["result_truncated"] for row in summary["members"]))
        for row in batch.expand_summary(summary)["members"]:
            self.assertEqual(row["result_text"], "\U0001f680" * len(row["result_text"]))
            self.assertIn("terminal_sha256", row)

    def test_missing_unknown_usage_stays_unknown(self):
        result = fixture(2)
        result["members"][1]["terminal"]["usage"] = None
        summary = batch.summarize(result)
        compact = batch._compact_summary(summary)
        self.assertNotIn("usage", compact["member_defaults"])
        self.assertIsNone(batch.expand_summary(compact)["members"][1]["usage"])

    def test_malformed_defaults_cannot_supply_member_authority_or_identity(self):
        compact = batch._compact_summary(batch.summarize(fixture(1)))
        for key in ("request_id", "artifact_root", "status", "first_typed_blocker",
                    "cleanup_pass", "evidence_status", "terminal_sha256"):
            value = copy.deepcopy(compact)
            value["member_defaults"][key] = "untrusted"
            with self.subTest(key=key), self.assertRaises(EmbeddedNokiyError):
                batch.expand_summary(value)

    def test_irreducible_metadata_remains_typed_failure_not_success(self):
        result = fixture()
        for i, row in enumerate(result["members"]):
            row["terminal"]["model_route"] = {"unique": str(i) * MAX_PROJECTED_BYTES}
        summary = batch.summarize(result)
        self.assertEqual(summary["status"], "COMPACT_METADATA_TOO_LARGE")
        self.assertEqual(summary["first_typed_blocker"], "NOKIY_BATCH_SUMMARY_TOO_LARGE")
        self.assertLessEqual(len(self.encoded(summary)), MAX_PROJECTED_BYTES)
