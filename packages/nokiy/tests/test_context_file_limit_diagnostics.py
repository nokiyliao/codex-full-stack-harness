# SPDX-License-Identifier: MIT
"""Exact-file count failures retain only actionable public count facts."""
import copy
import hashlib
import io
import json
import unittest
from unittest.mock import patch

import test_embedded_nokiy as fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import local_context as local


class ContextFileLimitDiagnosticsTests(unittest.TestCase):
    secret = "PRIVATE_CONTEXT_FILE_SENTINEL"
    issue = {"path": "action.read_scopes", "reason": "context_file_limit_exceeded"}

    def setUp(self):
        self.f = fixtures.EmbeddedNokiyFixture()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.names = [f"{self.secret}_{index:02}.txt" for index in range(37)]
        for name in self.names:
            (self.f.workspace / name).write_text(self.secret)

    def action(self, reads, *, writes=(), targets=()):
        return {
            "mission": {"mission_id": "count", "task_id": "local", "mode": "DELIVERY",
                        "objective": "Read bounded exact files", "current_predicate": "pending"},
            "context_summary": self.secret,
            "operations": ["read", "command"] + (["modify"] if writes else []),
            "read_scopes": list(reads), "write_scopes": list(writes),
            "target_paths": list(targets), "source_read": True,
        }

    def public_count(self, actual, maximum=32):
        return {"maximum": maximum, "actual": actual,
                "counted_scopes": ["read_scopes", "write_scopes", "target_paths"],
                "next_action": "narrow_exact_context_files"}

    def assert_count_failure(self, action, actual):
        original = copy.deepcopy(action)
        with self.assertRaises(caller.EmbeddedNokiyError) as caught:
            local.compile_context(self.f.workspace, action)
        failure = caller._failure(caught.exception)
        self.assertEqual(failure["issues"], [self.issue])
        self.assertEqual(failure["context_file_limit"], self.public_count(actual))
        self.assertEqual(caught.exception.actual, actual)
        self.assertEqual(caught.exception.maximum, 32)
        self.assertEqual(action, original)
        self.assertNotIn(self.secret, json.dumps(failure))
        return caught.exception

    def test_32_exact_files_pass_and_33_have_a_typed_count_failure(self):
        capsule, contract = local.compile_context(self.f.workspace, self.action(self.names[:32]))
        self.assertEqual(list(capsule["dcf_generation"]["source_snapshot"]), self.names[:32])
        self.assertEqual(contract["read_scopes"], self.names[:32])
        error = self.assert_count_failure(self.action(self.names[:33]), 33)
        self.assertIsInstance(error, caller.ContextFileLimitExceeded)

    def test_union_deduplicates_within_and_across_scopes_and_counts_target_only_files(self):
        action = self.action(self.names[:16] * 2, writes=self.names[8:24] * 2,
                             targets=self.names[8:32] * 2)
        capsule, contract = local.compile_context(self.f.workspace, action)
        self.assertEqual(list(capsule["dcf_generation"]["source_snapshot"]), self.names[:32])
        self.assertEqual(contract["write_scopes"], self.names[8:24])
        self.assertEqual(contract["declared_targets"], self.names[8:32])
        action["target_paths"].append(self.names[32])
        self.assert_count_failure(action, 33)

    def test_discovery_scopes_do_not_consume_exact_file_slots(self):
        (self.f.workspace / "discovery").mkdir()
        action = self.action(self.names[:32] + ["discovery/**"])
        action["read_directories"] = ["discovery"]
        # Pin real fixture bytes; this count test never executes discovery tools.
        with patch.object(local, "shutil", wraps=local.shutil) as tools:
            tools.which.return_value = str(self.f.codex)
            capsule, contract = local.compile_context(self.f.workspace, action)
        self.assertEqual(list(capsule["dcf_generation"]["source_snapshot"]), self.names[:32])
        self.assertIn("discovery/**", contract["read_scopes"])
        action["read_scopes"].append(self.names[32])
        self.assert_count_failure(action, 33)

    def test_empty_context_retains_the_legacy_generic_failure(self):
        action = self.action([])
        action.pop("source_read")
        action["operations"] = ["command"]
        with self.assertRaises(caller.EmbeddedNokiyError) as caught:
            local.compile_context(self.f.workspace, action)
        error = caught.exception
        self.assertEqual(type(error), caller.EmbeddedNokiyError)
        self.assertEqual(error.detail, "declare 1..32 exact context files")
        self.assertEqual(error.issues, [{"path": "action", "reason": "invalid_shape"}])
        self.assertNotIn("context_file_limit", caller._failure(error))

    def test_legacy_generic_errors_are_not_reinterpreted_from_attached_count_data(self):
        detail = "declare 1..32 exact context files"
        error = caller.EmbeddedNokiyError(local.CODE, detail,
                                         issues=[{"path": "action", "reason": "invalid_shape"}])
        error.actual, error.maximum = 37, 32
        error.context_file_limit = self.public_count(37)
        self.assertEqual(caller._failure(error), {
            "schema_version": caller.FAILURE_SCHEMA_VERSION, "status": "BLOCKED_PREEXECUTION",
            "first_typed_blocker": local.CODE,
            "detail_sha256": hashlib.sha256(detail.encode()).hexdigest(),
            "authority_effect": "none", "native_codex_control_plane_mutation_count": 0,
            "fallback_used": False, "issues": [{"path": "action", "reason": "invalid_shape"}],
        })

    def test_serializer_validates_integers_without_a_second_bound_or_private_fields(self):
        error = caller.ContextFileLimitExceeded(actual=37, maximum=12)
        error.detail = self.secret
        error.private_paths = self.names
        error.context_file_limit = {**self.public_count(37, 12), "private": self.secret}
        failure = caller._failure(error)
        self.assertEqual(failure["context_file_limit"], self.public_count(37, 12))
        self.assertEqual(failure["issues"], [self.issue])
        self.assertNotIn(self.secret, json.dumps(failure))
        for actual, maximum in ((37, True), (37, 0), (37, -1), (37, 32.0), (37, "32"),
                                (True, 1), (33.0, 32), (float("inf"), 32),
                                (float("nan"), 32), ("37", 32), (None, 32), (32, 32)):
            with self.subTest(actual=actual, maximum=maximum):
                malformed = caller.ContextFileLimitExceeded(actual=actual, maximum=maximum)
                self.assertNotIn("context_file_limit", caller._failure(malformed))
        error.code = "NOKIY_UNKNOWN_FAILURE"
        self.assertNotIn("context_file_limit", caller._failure(error))
        self.assertNotIn("issues", caller._failure(error))

    def test_37_overlapping_files_survive_cli_preparation_without_starting_a_provider(self):
        draft = json.loads(self.f.request_path.read_text())
        for key in ("context_capsule", "jspace_contract", "execution_profile"):
            draft.pop(key, None)
        self.f.request_path.write_text(json.dumps(draft))
        action = self.action(self.names, writes=self.names[:3], targets=self.names[:3])
        action_path = self.f.root / "count-action.json"
        action_path.write_text(json.dumps(action))
        original_bytes = action_path.read_bytes()
        output_dir = self.f.root / "prepared-count"
        output = io.StringIO()
        with (patch("sys.stdout", output),
              patch.object(caller, "execute", side_effect=AssertionError("no provider")) as execute,
              patch.object(caller, "preflight", side_effect=AssertionError("no preflight")) as preflight,
              patch.object(stack.subprocess, "run", side_effect=AssertionError("no process")) as process):
            code = caller.main(["prepare", "--request", str(self.f.request_path),
                                "--action", str(action_path), "--output-dir", str(output_dir), "--run"])
        for boundary in (execute, preflight, process):
            boundary.assert_not_called()
        self.assertEqual(code, 2)
        failure = json.loads(output.getvalue())
        self.assertEqual(failure["context_file_limit"], self.public_count(37))
        self.assertEqual(failure["issues"], [self.issue])
        self.assertEqual(failure["first_typed_blocker"], local.CODE)
        self.assertEqual(failure["status"], "BLOCKED_PREEXECUTION")
        self.assertEqual(failure["authority_effect"], "none")
        self.assertEqual(failure["native_codex_control_plane_mutation_count"], 0)
        self.assertIs(failure["fallback_used"], False)
        self.assertNotIn(self.secret, output.getvalue())
        self.assertNotIn(str(self.f.workspace), output.getvalue())
        self.assertEqual(action_path.read_bytes(), original_bytes)
        self.assertFalse(output_dir.exists())
