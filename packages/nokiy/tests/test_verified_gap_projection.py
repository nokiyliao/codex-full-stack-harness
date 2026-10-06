# SPDX-License-Identifier: MIT
"""Verified-trajectory handoff regressions; preview-only safety stays separate."""
import hashlib
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core, result_inspection as inspector
import test_result_inspection as fixtures


class VerifiedGapProjectionTests(unittest.TestCase):
    # Share receipt builders without inheriting or rerunning the legacy test methods.
    setUp = fixtures.InspectionTests.setUp
    publish = fixtures.InspectionTests.publish
    capability_handoff = fixtures.InspectionTests.capability_handoff
    capability_text = fixtures.InspectionTests.capability_text
    capability_fixture = fixtures.InspectionTests.capability_fixture
    project_capability = fixtures.InspectionTests.project_capability

    def complete_fixture(self, text):
        # Exercise a genuine request-bound small preview without raising any limit.
        with patch.object(caller, "MAX_RESULT_BYTES", 256):
            return self.capability_fixture(text)

    def long_answer(self, envelope):
        return "Leading ordinary prose.\n" * 400 + envelope + "Trailing ordinary prose.\n" * 400

    def read_events(self, run):
        return [json.loads(line) for line in (run / "core.jsonl").read_text().splitlines()]

    def write_events(self, terminal, run, events):
        trajectory = run / "core.jsonl"
        trajectory.write_bytes(b"".join(caller._canonical_bytes(event) + b"\n" for event in events))
        evidence = full_core._command_evidence(events)
        fixtures.store(run / "command-evidence.json", evidence)
        terminal.update(trajectory_artifact=fixtures.ref(trajectory),
                        command_evidence_artifact=fixtures.ref(run / "command-evidence.json"),
                        command_evidence_summary={key: evidence[key] for key in
                                                  ("total_count", "failed_count", "complete")})
        fixtures.store(run / "terminal.json", terminal)

    def rebind_budget(self, terminal, run, **changes):
        original = json.loads((run / "request-identity.json").read_text())
        original.update(changes)
        digest = caller._canonical_sha256({key: value for key, value in original.items()
                                          if key not in ("request_id", "request_sha256")})
        original.update(request_id="tura_embedded_" + digest, request_sha256=digest)
        destination = self.root / original["request_id"]
        run.rename(destination)
        terminal.update(request_id=original["request_id"], request_sha256=digest)
        for key, value in list(terminal.items()):
            if key.endswith("_artifact") and value is not None:
                terminal[key] = fixtures.ref(destination / Path(value["path"]).name)
        fixtures.store(destination / "request-identity.json", original)
        events = self.read_events(destination)
        events[-1].update(full_core._terminal_identity(original))
        self.write_events(terminal, destination, events)
        return destination

    def assert_unavailable(self, terminal, *, blocker=None, inspection_blocker=None, **kwargs):
        projected = self.project_capability(terminal, **kwargs)
        gap = projected["capability_gap"]
        self.assertEqual(gap["status"], "UNAVAILABLE")
        self.assertNotIn("handoff", gap)
        self.assertFalse(gap["permission_grant"])
        if blocker is not None:
            self.assertEqual(gap["first_blocker"], blocker)
        if inspection_blocker is not None:
            self.assertEqual(projected["result_inspection"]["first_blocker"], inspection_blocker)
        return projected

    def test_complete_message_handoff_survives_a_small_preview(self):
        data = self.capability_handoff()
        terminal, run, _ = self.complete_fixture(self.long_answer(self.capability_text(data)))
        self.assertTrue(terminal["result_truncated"])
        self.assertLessEqual(len(terminal["result_text"].encode("utf-8")), 256)
        self.assertNotIn(data["schema_version"], terminal["result_text"])
        evidence = inspector.inspect(self.root, terminal["request_id"], fixtures.THREAD)
        self.assertEqual(evidence["artifact_integrity"], "verified", evidence)
        self.assertEqual(evidence["first_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
        gap = evidence["capability_gap"]
        self.assertEqual(gap["status"], "MODEL_REPORTED")
        self.assertEqual(gap["handoff"], data)
        self.assertEqual(gap["binding"]["terminal_sha256"],
                         hashlib.sha256((run / "terminal.json").read_bytes()).hexdigest())

    def test_complete_envelope_forms_share_bindings_and_diagnostic_trust(self):
        data = self.capability_handoff()
        raw = json.dumps(data)
        for text in (self.long_answer(self.capability_text(raw)),
                     self.long_answer("```json\n" + raw + "\n```\n"),
                     " \t\n" * 100 + raw + "\n \t" * 100):
            with self.subTest(text=text[:30]):
                terminal, _, _ = self.complete_fixture(text)
                self.assertTrue(terminal["result_truncated"])
                projected = self.project_capability(terminal, request_thread_id=fixtures.THREAD,
                                                    require_request_binding=True)
                gap = projected["capability_gap"]
                self.assertEqual(gap["handoff"], data)
                self.assertEqual(gap["binding"]["request_sha256"], terminal["request_sha256"])
                self.assertEqual(gap["binding"]["native_thread_id"], fixtures.THREAD)
                self.assertEqual(gap["binding"]["terminal_content_sha256"], caller._canonical_sha256(terminal))
                self.assertTrue(gap["requires_parent_readmission"])
                self.assertFalse(gap["permission_grant"])
                self.assertFalse(gap["retry_safe"])
                self.assertEqual(gap["authority_effect"], "none")
                self.assertEqual(gap["execution_proof"], "unproven")
                self.assertEqual(gap["mission_acceptance"], "parent_owned")
                self.assertEqual(projected["result_inspection"]["first_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
                self.assertEqual({key: projected[key] for key in terminal}, terminal)

    def test_complete_malformed_duplicate_and_oversized_payloads_stay_unavailable(self):
        raw = json.dumps(self.capability_handoff())
        custom = self.capability_text(raw)
        duplicate_key = raw.replace('"schema_version":', '"schema_version":"other","schema_version":', 1)
        cases = [(self.capability_text(raw[:-1]), "CAPABILITY_GAP_INVALID_JSON"),
                 (self.capability_text(duplicate_key), "CAPABILITY_GAP_INVALID_JSON"),
                 (self.capability_text(raw.replace('"unified_diff":', '"unified_diff":NaN,"extra":', 1)),
                  "CAPABILITY_GAP_INVALID_JSON"),
                 (custom + custom, "CAPABILITY_GAP_DUPLICATE_FENCE"),
                 (custom + "```json\n" + raw + "\n```\n", "CAPABILITY_GAP_DUPLICATE_FENCE"),
                 (custom.removesuffix("```\n"), "CAPABILITY_GAP_FENCE_INVALID")]
        for envelope in (self.capability_text(raw + " " * inspector.MAX_CAPABILITY_GAP_BYTES),
                         "```json\n" + raw + " " * inspector.MAX_CAPABILITY_GAP_BYTES + "\n```\n",
                         self.capability_text(raw.replace("Reviewed", "é" * 4200))):
            cases.append((envelope, "CAPABILITY_GAP_TOO_LARGE"))
        for envelope, code in cases:
            with self.subTest(code=code, envelope=envelope[:60]):
                terminal, _, _ = self.complete_fixture(self.long_answer(envelope))
                projected = self.assert_unavailable(terminal, blocker=code)
                self.assertEqual(projected["result_inspection"]["artifact_integrity"], "verified")
                self.assertEqual(projected["result_inspection"]["first_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
        terminal, _, _ = self.complete_fixture(raw + " " * inspector.MAX_CAPABILITY_GAP_BYTES)
        self.assert_unavailable(terminal, blocker="CAPABILITY_GAP_TOO_LARGE")

    def test_complete_candidates_still_use_schema_path_and_diff_validation(self):
        data = self.capability_handoff()
        duplicate = data["missing_capabilities"] * 2
        cases = [(dict(data, extra=True), "CAPABILITY_GAP_SCHEMA_INVALID"),
                 (dict(data, completed_work=["x" * 513]), "CAPABILITY_GAP_SCHEMA_INVALID"),
                 (dict(data, missing_capabilities=duplicate), "CAPABILITY_GAP_SCHEMA_INVALID"),
                 (dict(data, unified_diff=None), "CAPABILITY_GAP_PATCH_OR_READ_EVIDENCE_REQUIRED"),
                 (dict(data, unified_diff=data["unified_diff"] + "x" * inspector.MAX_CAPABILITY_DIFF_BYTES + "\n"),
                  "CAPABILITY_GAP_DIFF_INVALID")]
        for path in ("../file.py", ".git/config", "src/*.py"):
            cases.append((dict(data, missing_capabilities=[{"path": path, "operation": "modify", "tool": "apply_patch"}]),
                          "CAPABILITY_GAP_PATH_INVALID"))
        for candidate, code in cases:
            with self.subTest(code=code, candidate=candidate):
                terminal, _, _ = self.complete_fixture(self.long_answer(self.capability_text(candidate)))
                self.assert_unavailable(terminal, blocker=code)
        read_only = dict(data, unified_diff=None,
                         missing_capabilities=[{"path": "src/file.py", "operation": "read", "tool": "source_read"}])
        terminal, _, _ = self.complete_fixture(self.long_answer(self.capability_text(read_only)))
        gap = self.project_capability(terminal)["capability_gap"]
        self.assertEqual(gap["handoff"], read_only)
        self.assertIsNone(gap["handoff"]["unified_diff"])

    def test_complete_handoff_does_not_borrow_missing_foreign_or_stale_request_bindings(self):
        text = self.long_answer(self.capability_text(self.capability_handoff()))
        for field, value in (("request_id", fixtures.RID), ("request_sha256", "f" * 64),
                             ("native_thread_id", "87654321-1234-1234-1234-123456789abc"),
                             ("max_result_bytes", 512)):
            with self.subTest(field=field):
                terminal, run, _ = self.complete_fixture(text)
                original = json.loads((run / "request-identity.json").read_text())
                original[field] = value
                fixtures.store(run / "request-identity.json", original)
                self.assert_unavailable(terminal, inspection_blocker="TRAJECTORY_REQUEST_IDENTITY_MISMATCH")
        terminal, run, _ = self.complete_fixture(text)
        (run / "request-identity.json").unlink()
        self.assert_unavailable(terminal, inspection_blocker="FILE_UNAVAILABLE")

    def test_complete_handoff_requires_the_current_caller_and_supplied_terminal_binding(self):
        terminal, _, _ = self.complete_fixture(self.long_answer(self.capability_text(self.capability_handoff())))
        self.assert_unavailable(terminal, require_request_binding=True,
                                inspection_blocker="REQUEST_THREAD_ID_INVALID")
        self.assert_unavailable(terminal, thread="87654321-1234-1234-1234-123456789abc",
                                request_thread_id=fixtures.THREAD, require_request_binding=True,
                                inspection_blocker="CALLER_THREAD_ID_MISMATCH")
        forged = dict(terminal, result_text=terminal["result_text"] + "forged")
        self.assert_unavailable(forged, blocker="CAPABILITY_GAP_TERMINAL_PROJECTION_MISMATCH")

    def test_complete_handoff_requires_exact_result_and_turn_identity(self):
        text = self.long_answer(self.capability_text(self.capability_handoff()))
        for kind in ("missing", "foreign", "stale", "size", "preview", "truncation"):
            with self.subTest(kind=kind):
                terminal, run, _ = self.complete_fixture(text)
                if kind == "missing":
                    terminal["result_artifact"] = None
                elif kind == "preview":
                    terminal["result_text"] = "foreign preview"
                elif kind == "truncation":
                    terminal["result_truncated"] = False
                else:
                    key, value = {"foreign": ("path", str(self.run / "last-message.txt")),
                                  "stale": ("sha256", "f" * 64), "size": ("bytes", 1)}[kind]
                    terminal["result_artifact"][key] = value
                fixtures.store(run / "terminal.json", terminal)
                if kind == "truncation":
                    # The forged untruncated preview contains no envelope at all;
                    # withholding the diagnostic is also unavailable, not a handoff.
                    projected = self.project_capability(terminal)
                    self.assertEqual(projected["result_inspection"]["first_blocker"], "NOKIY_FULL_CORE_RESULT_INVALID")
                    self.assertNotIn("handoff", projected.get("capability_gap", {}))
                else:
                    self.assert_unavailable(terminal, inspection_blocker="NOKIY_FULL_CORE_RESULT_INVALID")
        for kind in ("turn", "usage", "missing_turn"):
            with self.subTest(kind=kind):
                terminal, run, _ = self.complete_fixture(text)
                events = self.read_events(run)
                if kind == "turn":
                    events[-1]["session_id"] = "foreign-session"
                elif kind == "usage":
                    events[-1]["usage"] = {"input_tokens": 99}
                else:
                    events.pop()
                self.write_events(terminal, run, events)
                expected = {"turn": "NOKIY_FULL_CORE_TERMINAL_IDENTITY_MISMATCH",
                            "usage": "TRAJECTORY_USAGE_MISMATCH", "missing_turn": "NOKIY_FULL_CORE_TERMINAL_INVALID"}[kind]
                self.assert_unavailable(terminal, inspection_blocker=expected)

    def test_tampered_missing_and_symlinked_artifacts_cannot_release_a_candidate(self):
        text = self.long_answer(self.capability_text(self.capability_handoff()))
        for name in ("core.jsonl", "last-message.txt", "request-identity.json"):
            for kind in ("tampered", "missing", "symlink"):
                with self.subTest(name=name, kind=kind):
                    terminal, run, _ = self.complete_fixture(text)
                    path = run / name
                    before = path.read_bytes()
                    if kind == "tampered":
                        if name == "request-identity.json":
                            original = json.loads(before)
                            original["workspace"] += "/foreign"
                            fixtures.store(path, original)
                        else:
                            path.write_bytes(before.replace(b"Leading", b"Altered", 1))
                    else:
                        path.unlink()
                        if kind == "symlink":
                            target = self.root / (name + ".physical")
                            target.write_bytes(before)
                            path.symlink_to(target)
                    try:
                        projected = self.assert_unavailable(terminal)
                        self.assertEqual(projected["result_inspection"]["artifact_integrity"], "unproven")
                    finally:
                        if path.is_symlink():
                            path.unlink()
                        path.write_bytes(before)

    def test_request_budgets_still_fail_closed(self):
        text = self.long_answer(self.capability_text(self.capability_handoff()))
        for changes, code in (({"max_trajectory_bytes": 1024}, "NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED"),
                              ({"max_trajectory_bytes": 512}, "TRAJECTORY_BUDGET_INVALID"),
                              ({"max_result_bytes": 0}, "RESULT_BUDGET_INVALID"),
                              ({"max_result_bytes": caller.MAX_RESULT_BYTES + 1}, "RESULT_BUDGET_INVALID")):
            with self.subTest(changes=changes):
                terminal, run, _ = self.complete_fixture(text)
                self.rebind_budget(terminal, run, **changes)
                self.assert_unavailable(terminal, inspection_blocker=code)

    def test_drift_after_complete_decode_still_withholds_the_handoff(self):
        text = self.long_answer(self.capability_text(self.capability_handoff()))
        original_projection = full_core._result_projection
        for kind, code in (("trajectory", "ARTIFACT_CHANGED"), ("terminal", "TERMINAL_CHANGED"),
                           ("command_evidence", "ARTIFACT_HASH_MISMATCH")):
            with self.subTest(kind=kind):
                terminal, run, _ = self.complete_fixture(text)
                def drift_after_result_check(*args):
                    result = original_projection(*args)
                    if kind == "trajectory":
                        path = run / "core.jsonl"
                        path.write_bytes(path.read_bytes().replace(b"Leading", b"Altered", 1))
                    elif kind == "terminal":
                        fixtures.store(run / "terminal.json", dict(terminal, first_typed_blocker="DRIFTED"))
                    else:
                        fixtures.store(run / "command-evidence.json", {"complete": False})
                    return result
                with patch.object(full_core, "_result_projection", drift_after_result_check):
                    self.assert_unavailable(terminal, inspection_blocker=code)

    def test_only_the_last_completed_message_outcome_survives(self):
        valid = self.long_answer(self.capability_text(self.capability_handoff()))
        absent = self.long_answer("No patch or capability handoff was supplied.\n")
        invalid = self.long_answer(self.capability_text("{"))
        invalid_schema = self.long_answer(self.capability_text(dict(self.capability_handoff(), extra=True)))
        for earlier, last, expected in ((valid, absent, None), (valid, "Final answer without a handoff.", None),
                                        (valid, invalid, "CAPABILITY_GAP_INVALID_JSON"),
                                        (valid, invalid_schema, "CAPABILITY_GAP_SCHEMA_INVALID"),
                                        (invalid, valid, "MODEL_REPORTED")):
            with self.subTest(expected=expected):
                terminal, run, _ = self.complete_fixture(last)
                events = self.read_events(run)
                events.insert(0, {"type": "item.completed", "item": {"type": "assistant_message", "text": earlier}})
                # A later non-completed message also cannot supply/replace the final outcome.
                events.insert(-1, {"type": "item.updated", "item": {"type": "assistant_message", "text": valid}})
                self.write_events(terminal, run, events)
                projected = self.project_capability(terminal)
                self.assertEqual(projected["result_inspection"]["artifact_integrity"], "verified")
                if expected is None:
                    self.assertNotIn("capability_gap", projected)
                elif expected != "MODEL_REPORTED":
                    self.assertEqual(projected["capability_gap"]["status"], "UNAVAILABLE")
                    self.assertNotIn("handoff", projected["capability_gap"])
                    self.assertEqual(projected["capability_gap"]["first_blocker"], expected)
                else:
                    self.assertEqual(projected["capability_gap"]["handoff"], self.capability_handoff())

    def test_ordinary_large_results_do_not_fabricate_a_patch_or_capability(self):
        for text in ("Ordinary large prose.\n" * 10000,
                     self.long_answer("```json\n{\"status\":\"ok\"}\n```\n"),
                     self.long_answer("A patch will be provided later; none is present.\n")):
            with self.subTest(text=text[:40]):
                terminal, _, _ = self.complete_fixture(text)
                projected = self.project_capability(terminal)
                self.assertTrue(terminal["result_truncated"])
                self.assertEqual(projected["result_inspection"]["artifact_integrity"], "verified")
                self.assertNotIn("capability_gap", projected)

    def test_projection_ceiling_does_not_replace_the_first_execution_blocker(self):
        terminal, _, _ = self.complete_fixture(self.long_answer(self.capability_text(self.capability_handoff())))
        projected = self.project_capability(terminal)
        baseline = {key: value for key, value in projected.items() if key != "capability_gap"}
        limit = (len(caller._canonical_bytes(baseline)) + 32 +
                 len(caller._canonical_bytes(inspector._gap_diagnostic("CAPABILITY_GAP_PROJECTION_TOO_LARGE"))))
        with patch.object(inspector, "MAX_PROJECTED_BYTES", limit):
            bounded = self.assert_unavailable(terminal, blocker="CAPABILITY_GAP_PROJECTION_TOO_LARGE")
        self.assertLessEqual(len(caller._canonical_bytes(bounded)), limit)
        self.assertEqual(bounded["result_inspection"]["first_blocker"], projected["result_inspection"]["first_blocker"])
        self.assertEqual({key: bounded[key] for key in terminal}, terminal)

    def test_blocked_terminal_cannot_publish_an_unverified_complete_outcome(self):
        terminal, run, _ = self.complete_fixture(self.long_answer(self.capability_text(self.capability_handoff())))
        terminal.update(status="BLOCKED", first_typed_blocker="ORIGINAL_SCOPE_DENIED")
        fixtures.store(run / "terminal.json", terminal)
        projected = self.assert_unavailable(terminal, blocker="CAPABILITY_GAP_RESULT_TRUNCATED")
        self.assertEqual(projected["status"], "BLOCKED")
        self.assertEqual(projected["first_typed_blocker"], "ORIGINAL_SCOPE_DENIED")

    def test_handoff_adds_no_result_read_scan_execution_or_durable_mutation(self):
        text = self.long_answer(self.capability_text(self.capability_handoff()))
        terminal, run, workspace = self.complete_fixture(text)
        before_terminal = deepcopy(terminal)
        before = {path: path.read_bytes() for path in run.iterdir() if path.is_file()}
        source_before = (workspace / "src/file.py").read_bytes()
        counts = Counter()
        original_iter = full_core._Trajectory.__iter__
        original_enter = full_core._ArtifactReader.__enter__
        original_decode = inspector._gap_envelope
        def iter_trajectory(trajectory):
            counts["trajectory_passes"] += 1
            yield from original_iter(trajectory)
        def enter_reader(reader):
            counts[reader.path.name] += 1
            return original_enter(reader)
        def decode_complete(value):
            self.assertEqual(value, text)
            self.assertNotEqual(value, terminal["result_text"])
            counts["complete_message_decode"] += 1
            return original_decode(value)
        with patch.object(full_core._Trajectory, "__iter__", iter_trajectory), \
                patch.object(full_core._ArtifactReader, "__enter__", enter_reader), \
                patch.object(inspector, "_gap_envelope", decode_complete), \
                patch.object(inspector, "inspect", wraps=inspector.inspect) as inspect_call, \
                patch.object(caller, "execute", side_effect=AssertionError("no second execution")) as execute, \
                patch.object(Path, "read_text", side_effect=AssertionError("no bulk read")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no bulk read")):
            projected = self.project_capability(terminal)
        self.assertEqual(inspect_call.call_count, 1)
        execute.assert_not_called()
        self.assertEqual(counts["trajectory_passes"], 2)  # Existing summary and command-proof passes.
        self.assertEqual(counts["last-message.txt"], 1)  # Existing integrity read only.
        self.assertEqual(counts["complete_message_decode"], 1)
        self.assertEqual(projected["capability_gap"]["handoff"], self.capability_handoff())
        with patch.object(inspector, "inspect", side_effect=AssertionError("no second inspection")):
            summary = inspector.summarize_terminal(projected, self.root, terminal["request_id"])
        self.assertEqual(summary["capability_gap"], projected["capability_gap"])
        self.assertEqual(terminal, before_terminal)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual((workspace / "src/file.py").read_bytes(), source_before)
