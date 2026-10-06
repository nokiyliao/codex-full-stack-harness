"""Offline summary contract: metadata compaction never grants result acceptance."""
import copy
import json
import unittest
from unittest.mock import patch
from codex_collaboration_harness import batch
from codex_collaboration_harness.embedded_nokiy import EmbeddedNokiyError


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

    def test_five_members_keep_all_identity_proof_usage_and_failure(self):
        result = fixture()
        original = copy.deepcopy(result)
        summary = batch.summarize(result)
        self.assertEqual(summary["schema_version"], "nokiy_direct_batch_summary_v2")
        self.assertLessEqual(len(self.encoded(summary)), 8000)
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
        self.assertLessEqual(len(self.encoded(summary)), 8000)
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
            row["terminal"]["model_route"] = {"unique": str(i) * 10000}
        summary = batch.summarize(result)
        self.assertEqual(summary["status"], "COMPACT_METADATA_TOO_LARGE")
        self.assertEqual(summary["first_typed_blocker"], "NOKIY_BATCH_SUMMARY_TOO_LARGE")
        self.assertLessEqual(len(self.encoded(summary)), 8000)
