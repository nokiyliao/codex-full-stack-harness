# SPDX-License-Identifier: MIT
"""Envelope regressions using the immutable InspectionTests receipt fixtures."""
import hashlib
import json
from copy import deepcopy
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import result_inspection as inspector
import test_result_inspection as fixtures


class CapabilityGapEnvelopeTests(unittest.TestCase):
    # Reuse fixtures without inheriting and rerunning the pinned suite's test methods.
    setUp = fixtures.InspectionTests.setUp
    publish = fixtures.InspectionTests.publish
    capability_handoff = fixtures.InspectionTests.capability_handoff
    capability_text = fixtures.InspectionTests.capability_text
    capability_fixture = fixtures.InspectionTests.capability_fixture
    project_capability = fixtures.InspectionTests.project_capability

    def envelopes(self, raw):
        return (("custom", self.capability_text(raw)),
                ("json", "```json\n" + raw + "\n```\n"),
                ("raw", raw))

    def assert_unavailable(self, text, blocker=None):
        terminal, _, _ = self.capability_fixture(text)
        gap = self.project_capability(terminal)["capability_gap"]
        self.assertEqual(gap["status"], "UNAVAILABLE")
        self.assertNotIn("handoff", gap)
        self.assertFalse(gap["permission_grant"])
        if blocker is not None:
            self.assertEqual(gap["first_blocker"], blocker)
        return terminal, gap

    def test_equivalent_envelopes_share_validation_binding_and_no_effects(self):
        data = self.capability_handoff()
        for kind, text in self.envelopes(json.dumps(data)):
            with self.subTest(kind=kind):
                terminal, run, workspace = self.capability_fixture(text)
                before_terminal = deepcopy(terminal)
                before = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
                source = (workspace / "src/file.py").read_bytes()
                with patch.object(caller, "execute", side_effect=AssertionError("no execution")):
                    projected = self.project_capability(
                        terminal, request_thread_id=fixtures.THREAD, require_request_binding=True)
                gap = projected["capability_gap"]
                self.assertEqual(gap["status"], "MODEL_REPORTED")
                self.assertEqual(gap["handoff"], data)
                self.assertEqual(gap["binding"]["request_id"], terminal["request_id"])
                self.assertEqual(gap["binding"]["request_sha256"], terminal["request_sha256"])
                self.assertEqual(gap["binding"]["native_thread_id"], fixtures.THREAD)
                self.assertEqual(gap["binding"]["terminal_sha256"],
                                 hashlib.sha256(before[run / "terminal.json"]).hexdigest())
                self.assertEqual(gap["binding"]["terminal_content_sha256"],
                                 caller._canonical_sha256(terminal))
                self.assertTrue(gap["requires_parent_readmission"])
                self.assertFalse(gap["permission_grant"])
                self.assertFalse(gap["retry_safe"])
                self.assertEqual(gap["authority_effect"], "none")
                self.assertEqual(gap["execution_proof"], "unproven")
                self.assertEqual(gap["mission_acceptance"], "parent_owned")
                self.assertEqual(projected["result_inspection"]["first_blocker"],
                                 "EXECUTION_PROOF_UNAVAILABLE")
                self.assertEqual(terminal, before_terminal)
                self.assertEqual({key: projected[key] for key in terminal}, terminal)
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                self.assertEqual((workspace / "src/file.py").read_bytes(), source)
                self.assertNotIn("capability_gap", caller.read_terminal(self.root, terminal["request_id"]))

    def test_raw_json_whitespace_and_json_fence_with_prose(self):
        data = self.capability_handoff()
        raw = json.dumps(data, indent=2)
        for text in (" \t\r\n" + raw + "\r\n\t ",
                     "First blocker: missing capability.\n```json\n" + raw + "\n```\nParent must review.\n"):
            with self.subTest(text=text[:60]):
                terminal, _, _ = self.capability_fixture(text)
                self.assertEqual(self.project_capability(terminal)["capability_gap"]["handoff"], data)

    def test_unrelated_json_is_a_no_op_even_without_caller_binding(self):
        data = self.capability_handoff()
        for value in ({"status": "ok"}, {"schema_version": "other"}, {"wrapper": data},
                      [data], "ordinary answer", True, None):
            raw = json.dumps(value)
            for text in (raw, "```json\n" + raw + "\n```\n"):
                with self.subTest(value=value, text=text[:30]):
                    terminal, _, _ = self.capability_fixture(text)
                    self.assertNotIn("capability_gap", self.project_capability(terminal))
                    self.assertNotIn("capability_gap", self.project_capability(terminal, thread="invalid"))

    def test_malformed_duplicate_key_and_nonfinite_json_are_rejected(self):
        raw = json.dumps(self.capability_handoff())
        cases = (raw[:-1], raw + "\n" + raw,
                 raw.replace('"schema_version":', '"schema_version":"other","schema_version":', 1),
                 raw.replace('"tool":', '"tool":"duplicate","tool":', 1),
                 raw.replace('"completed_work":', '"completed_work":NaN,"ignored":', 1),
                 raw.replace('"completed_work":', '"completed_work":Infinity,"ignored":', 1))
        for invalid in cases:
            for kind, text in self.envelopes(invalid):
                with self.subTest(kind=kind, invalid=invalid[:60]):
                    self.assert_unavailable(text, "CAPABILITY_GAP_INVALID_JSON")

    def test_unrelated_malformed_json_keeps_absence_a_no_op(self):
        for text in ("{", "{ordinary text", "```json\n{\n```\n", "```json\n{\n"):
            with self.subTest(text=text):
                terminal, _, _ = self.capability_fixture(text)
                self.assertNotIn("capability_gap", self.project_capability(terminal))
                self.assertNotIn("capability_gap", self.project_capability(terminal, thread="invalid"))
        self.assert_unavailable(self.capability_text("{"), "CAPABILITY_GAP_INVALID_JSON")

    def test_oversized_payloads_include_raw_surrounding_whitespace(self):
        raw = json.dumps(self.capability_handoff())
        for invalid in (raw + " " * inspector.MAX_CAPABILITY_GAP_BYTES,
                        " " * inspector.MAX_CAPABILITY_GAP_BYTES + raw):
            for kind, text in self.envelopes(invalid):
                with self.subTest(kind=kind):
                    _, gap = self.assert_unavailable(text)
                    self.assertEqual(gap["first_blocker"], "CAPABILITY_GAP_TOO_LARGE")

    def test_duplicate_and_ambiguous_handoffs_are_not_salvaged(self):
        raw = json.dumps(self.capability_handoff())
        custom = self.capability_text(raw)
        ordinary = "```json\n" + raw + "\n```\n"
        unrelated = '```json\n{"status":"ok"}\n```\n'
        for text in (custom + custom, ordinary + ordinary, custom + ordinary, ordinary + custom,
                     ordinary + unrelated + ordinary):
            with self.subTest(text=text[:50]):
                self.assert_unavailable(text, "CAPABILITY_GAP_DUPLICATE_FENCE")
        self.assert_unavailable(ordinary + "```json\n{\n```\n", "CAPABILITY_GAP_INVALID_JSON")
        self.assert_unavailable("```json\n{\n```\n" + ordinary, "CAPABILITY_GAP_INVALID_JSON")
        self.assert_unavailable(ordinary.removesuffix("```\n"), "CAPABILITY_GAP_FENCE_INVALID")

    def test_raw_json_must_be_the_entire_answer_and_prose_is_not_inferred(self):
        raw = json.dumps(self.capability_handoff())
        for text in ("Here is a proposal:\n" + raw, raw + "\nI will apply it later.",
                     "inline ```json\n" + raw + "\n```\n"):
            with self.subTest(text=text[:40]):
                terminal, _, _ = self.capability_fixture(text)
                self.assertNotIn("handoff", self.project_capability(terminal).get("capability_gap", {}))

    def test_supplied_no_patch_prose_and_patch_promises_are_no_ops(self):
        for text in ("The supplied code returns **6**, while `verify.py` requires **7**. "
                     "The first blocker is the denied `modify` operation on `counter.py`. "
                     "No edits or tests were performed; the patch below is for parent-owned continuation.",
                     "V119 returned only prose promising a patch; it MUST remain no usable handoff.",
                     "I will propose a patch after reviewing the source.",
                     "First blocker: missing capability. I will provide a unified diff next.",
                     "nokiy_capability_gap_v1: I will supply the missing patch later."):
            with self.subTest(text=text):
                terminal, _, _ = self.capability_fixture(text)
                self.assertNotIn("capability_gap", self.project_capability(terminal))

    def test_missing_patch_and_incomplete_schema_are_not_fabricated(self):
        base = self.capability_handoff()
        absent = dict(base)
        del absent["unified_diff"]
        for data, blocker in ((dict(base, unified_diff=None), "CAPABILITY_GAP_PATCH_OR_READ_EVIDENCE_REQUIRED"),
                              (absent, "CAPABILITY_GAP_SCHEMA_INVALID"),
                              (dict(base, unified_diff="patch promised"), "CAPABILITY_GAP_DIFF_INVALID")):
            for kind, text in self.envelopes(json.dumps(data)):
                with self.subTest(kind=kind, blocker=blocker):
                    self.assert_unavailable(text, blocker)

    def test_schema_work_capability_and_diff_limits_stay_shared(self):
        base = self.capability_handoff()
        for data in (dict(base, extra=True), dict(base, completed_work=["x" * 513]),
                     dict(base, missing_capabilities=base["missing_capabilities"] * 2),
                     dict(base, remaining_work=[])):
            for kind, text in self.envelopes(json.dumps(data)):
                with self.subTest(kind=kind, data=data):
                    self.assert_unavailable(text, "CAPABILITY_GAP_SCHEMA_INVALID")
        data = dict(base, unified_diff=base["unified_diff"] + "x" * inspector.MAX_CAPABILITY_DIFF_BYTES + "\n")
        for kind, text in self.envelopes(json.dumps(data)):
            with self.subTest(kind=kind):
                self.assert_unavailable(text, "CAPABILITY_GAP_DIFF_INVALID")

    def test_dangerous_paths_in_reports_and_diff_headers_are_rejected(self):
        for path in ("/tmp/out.py", "../out.py", "src/../out.py", "./src/file.py", "src//file.py",
                     "src/*.py", ".git/config", "src/.git/config"):
            data = self.capability_handoff()
            data["missing_capabilities"][0]["path"] = path
            data["unified_diff"] = None
            for kind, text in self.envelopes(json.dumps(data)):
                with self.subTest(kind=kind, path=path):
                    self.assert_unavailable(text, "CAPABILITY_GAP_PATH_INVALID")
        data = self.capability_handoff()
        data["unified_diff"] = "--- a/../out.py\n+++ b/../out.py\n@@ -1 +1 @@\n-old\n+new\n"
        for kind, text in self.envelopes(json.dumps(data)):
            with self.subTest(kind=kind):
                self.assert_unavailable(text, "CAPABILITY_GAP_PATH_INVALID")

    def test_symlink_paths_are_rejected_in_new_envelopes(self):
        for kind in ("json", "raw"):
            for path in ("src/link.py", "linkdir/file.py"):
                with self.subTest(kind=kind, path=path):
                    data = self.capability_handoff()
                    data["missing_capabilities"][0]["path"] = path
                    data["unified_diff"] = None
                    text = dict(self.envelopes(json.dumps(data)))[kind]
                    terminal, _, workspace = self.capability_fixture(text)
                    link = workspace / ("src/link.py" if path.startswith("src/") else "linkdir")
                    if link.is_symlink():
                        link.unlink()
                    link.symlink_to(self.root / "foreign")
                    gap = self.project_capability(terminal)["capability_gap"]
                    self.assertEqual(gap["first_blocker"], "CAPABILITY_GAP_PATH_UNSAFE")
                    self.assertNotIn("handoff", gap)

    def test_forged_caller_request_and_terminal_identity_are_rejected(self):
        for kind, text in self.envelopes(json.dumps(self.capability_handoff())):
            with self.subTest(kind=kind):
                terminal, run, _ = self.capability_fixture(text)
                projected = self.project_capability(
                    terminal, thread="87654321-1234-1234-1234-123456789abc",
                    request_thread_id=fixtures.THREAD, require_request_binding=True)
                self.assertEqual(projected["result_inspection"]["first_blocker"], "CALLER_THREAD_ID_MISMATCH")
                self.assertEqual(projected["capability_gap"]["status"], "UNAVAILABLE")
                self.assertNotIn("handoff", projected["capability_gap"])
                forged = dict(terminal, result_text=terminal["result_text"] + "forged")
                self.assertEqual(self.project_capability(forged)["capability_gap"]["first_blocker"],
                                 "CAPABILITY_GAP_TERMINAL_PROJECTION_MISMATCH")
                identity = json.loads((run / "request-identity.json").read_text())
                identity["native_thread_id"] = "87654321-1234-1234-1234-123456789abc"
                fixtures.store(run / "request-identity.json", identity)
                projected = self.project_capability(terminal)
                self.assertEqual(projected["result_inspection"]["first_blocker"],
                                 "TRAJECTORY_REQUEST_IDENTITY_MISMATCH")
                self.assertEqual(projected["capability_gap"]["status"], "UNAVAILABLE")
                self.assertNotIn("handoff", projected["capability_gap"])

    def test_truncated_previews_are_never_decoded(self):
        data = self.capability_handoff()
        decode = inspector._gap_envelope
        for kind, text in self.envelopes(json.dumps(data)):
            with self.subTest(kind=kind):
                complete = text + "x" * (caller.MAX_RESULT_BYTES + 100)
                terminal, _, _ = self.capability_fixture(complete)
                self.assertTrue(terminal["result_truncated"])
                def complete_only(value):
                    self.assertEqual(value, complete)
                    self.assertNotEqual(value, terminal["result_text"])
                    return decode(value)
                with patch.object(inspector, "_gap_envelope", side_effect=complete_only) as parsed:
                    gap = self.project_capability(terminal)["capability_gap"]
                parsed.assert_called_once_with(complete)
                self.assertFalse(gap["permission_grant"])
                if kind == "raw":
                    self.assertEqual(gap["first_blocker"], "CAPABILITY_GAP_TOO_LARGE")
                    self.assertNotIn("handoff", gap)
                else:
                    self.assertEqual(gap["status"], "MODEL_REPORTED")
                    self.assertEqual(gap["handoff"], data)
                    self.assertTrue(gap["requires_parent_readmission"])

    def test_custom_fence_errors_are_preserved(self):
        custom = self.capability_text(self.capability_handoff())
        self.assert_unavailable(custom.replace("\n```nokiy", " inline ```nokiy"), "CAPABILITY_GAP_FENCE_INVALID")
        self.assert_unavailable(custom.removesuffix("```\n"), "CAPABILITY_GAP_FENCE_INVALID")
        self.assert_unavailable(custom + custom, "CAPABILITY_GAP_DUPLICATE_FENCE")
        other = dict(self.capability_handoff(), schema_version="other")
        self.assert_unavailable(self.capability_text(other), "CAPABILITY_GAP_SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()
