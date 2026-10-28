# SPDX-License-Identifier: MIT
"""Preparation rejects unchanged while its JSON retains only safe typed causes."""
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


class LocalContextDiagnosticsTests(unittest.TestCase):
    secret = "LOCAL_CONTEXT_PRIVATE_PATH_COMMAND_AND_SOURCE_SENTINEL"

    def setUp(self):
        # Diagnostics of exact preparation: the read-only fallback that reprepares a
        # refused action as inspect is covered by test_full_stack.ReadOnlyRouteTests.
        exact_only = patch.object(stack, "_read_commands_as_inspect", return_value=None)
        exact_only.start()
        self.addCleanup(exact_only.stop)
        self.f = fixtures.EmbeddedNokiyFixture()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.draft = json.loads(self.f.request_path.read_text())
        for key in ("context_capsule", "jspace_contract", "execution_profile"):
            self.draft.pop(key, None)
        self.draft["prompt"] = self.secret
        (self.f.workspace / "input.txt").write_text(self.secret)
        self.action = {
            "mission": {"mission_id": "diagnostics", "task_id": "local", "mode": "DELIVERY",
                        "objective": "Read exact input", "current_predicate": "input_read"},
            "context_summary": self.secret,
            "operations": ["read", "command"], "read_scopes": ["input.txt"],
            "write_scopes": [], "target_paths": ["input.txt"],
            "command_templates": [{"argv": ["cat", "input.txt"], "effects": ["read"],
                                   "targets": [{"operation": "read", "path": "input.txt", "argv_index": 1}]}],
        }
        self.action_path = self.f.root / "action-local.json"
        self.output = self.f.root / "prepared-local"

    def assert_prepare_failure(self, action, path, reason, *, blocker=local.CODE, additional_issues=()):
        self.f.request_path.write_text(json.dumps(self.draft))
        self.action_path.write_text(json.dumps(action))
        output = io.StringIO()
        with (patch("sys.stdout", output),
              patch.object(caller, "execute", side_effect=AssertionError("no model")) as execute,
              patch.object(caller, "preflight", side_effect=AssertionError("no preflight")) as preflight,
              patch.object(stack.subprocess, "run", side_effect=AssertionError("no process")) as process):
            code = caller.main(["prepare", "--request", str(self.f.request_path),
                                "--action", str(self.action_path), "--output-dir", str(self.output), "--run"])
        for boundary in (execute, preflight, process):
            boundary.assert_not_called()
        raw = output.getvalue()
        failure = json.loads(raw)
        self.assertEqual(code, 2)
        self.assertEqual(failure["status"], "BLOCKED_PREEXECUTION")
        self.assertEqual(failure["first_typed_blocker"], blocker)
        self.assertEqual(failure["issues"], [{"path": path, "reason": reason}, *additional_issues])
        self.assertEqual(failure["authority_effect"], "none")
        self.assertEqual(failure["native_codex_control_plane_mutation_count"], 0)
        self.assertIs(failure["fallback_used"], False)
        self.assertNotIn("detail", failure)
        self.assertNotIn(self.secret, raw)
        self.assertNotIn(str(self.f.workspace), raw)
        self.assertFalse(self.output.exists())
        return failure

    def test_invalid_scope_and_operation_lists_retain_their_field(self):
        for field in ("operations", "read_scopes", "write_scopes", "target_paths",
                      "read_directories", "forbidden_effects"):
            # A null value is omission, not an invalid list (V430).
            for value in (self.secret, [True], [" "], ["input.txt"] * 65):
                with self.subTest(field=field, value=value):
                    action = copy.deepcopy(self.action)
                    action[field] = value
                    self.assert_prepare_failure(action, "action." + field, "invalid_string_list")

    def test_scope_and_permission_contradictions_still_reject(self):
        cases = (
            ({"operations": ["rename"]}, "action.operations", "unsupported_operation"),
            ({"operations": []}, "action.operations", "unsupported_operation"),
            ({"operations": ["command"]}, "action.operations", "read_operation_missing"),
            # V430: exact declared writes take their write operations; the target rule remains.
            ({"write_scopes": [self.secret]}, "action.write_scopes", "not_in_target_paths"),
            ({"operations": ["read", "command", "modify"]}, "action.write_scopes", "write_scope_missing"),
            ({"forbidden_effects": ["read"]}, "action.forbidden_effects", "scope_mismatch"),
            ({"forbidden_effects": ["write"], "operations": ["read", "command", "modify"],
              "write_scopes": ["input.txt"]}, "action.forbidden_effects", "scope_mismatch"),
            ({"source_read": self.secret}, "action.source_read", "invalid_shape"),
            ({"source_read": True, "operations": ["read"]}, "action.source_read", "scope_mismatch"),
            ({"source_read": True, "read_scopes": [" " + self.secret]},
             "action.read_scopes", "unsafe_path"),
        )
        for changes, path, reason, *additional_issues in cases:
            with self.subTest(changes=changes):
                action = copy.deepcopy(self.action)
                action.update(changes)
                self.assert_prepare_failure(action, path, reason, additional_issues=additional_issues)

        # V431: with the request artifact root a reading action gains the read-only parent shell,
        # whose command operation also admits the declared reader instead of refusing the omission.
        reader_only = dict(self.action, operations=["read"])
        with self.assertRaises(caller.EmbeddedNokiyError) as caught:
            local.compile_context(self.f.workspace, reader_only)
        self.assertEqual(caught.exception.issues,
                         [{"path": "action.operations", "reason": "command_operation_missing"}])
        artifacts = self.f.root / "shell-artifacts"
        artifacts.mkdir()
        _, contract = local.compile_context(self.f.workspace, reader_only, artifact_root=artifacts)
        self.assertEqual(contract["allowed_operations"], ["command", "read"])
        self.assertEqual(contract["parent_shell"]["schema_version"], "nokiy_parent_shell_v1")
        self.assertEqual(contract["write_scopes"], [])

        # The compiler still rejects an unused command grant; CLI input now supplies its reader.
        action = dict(self.action, command_templates=[])
        with self.assertRaises(caller.EmbeddedNokiyError) as caught:
            local.compile_context(self.f.workspace, action)
        self.assertEqual(caught.exception.code, local.CODE)
        self.assertEqual(caught.exception.issues,
                         [{"path": "action.operations", "reason": "command_without_templates"}])
        adapted, repairs = stack._normalize_action_input(action)
        self.assertEqual(repairs, [{"path": "action.source_read", "rule": "command_without_reader_to_source_read"}])
        self.assertIs(adapted["source_read"], True)
        capsule, contract = local.compile_context(self.f.workspace, adapted)
        local.verify_context(self.f.workspace, capsule, contract)
        self.assertEqual(contract["allowed_operations"], ["command", "read"])
        self.assertEqual(contract["read_scopes"], ["input.txt"])
        self.assertEqual(contract["write_scopes"], [])
        self.assertEqual(contract["command_templates"], [])

    def test_unsafe_exact_scopes_retain_field_without_echoing_paths(self):
        (self.f.workspace / "linked").symlink_to(self.f.workspace / "input.txt")
        for field in ("read_scopes", "write_scopes", "target_paths"):
            for name in ("../" + self.secret, str(self.f.root / self.secret),
                         ".git/" + self.secret, "linked"):
                with self.subTest(field=field, name=name):
                    action = copy.deepcopy(self.action)
                    action[field] = [name]
                    if field == "write_scopes":
                        action["operations"].append("modify")
                        action["target_paths"].append(name)
                    self.assert_prepare_failure(action, "action." + field, "unsafe_path")

    def test_permission_and_bounded_regular_file_failures_retain_field(self):
        (self.f.workspace / "directory").mkdir()
        action = copy.deepcopy(self.action)
        action["read_scopes"] = ["directory"]
        self.assert_prepare_failure(action, "action.read_scopes", "exact_file_unavailable")
        for field in ("read_scopes", "write_scopes", "target_paths"):
            with self.subTest(field=field):
                action = copy.deepcopy(self.action)
                name = "0-" + self.secret
                action[field] = [name]
                if field == "write_scopes":
                    action["operations"].append("modify")
                    action["target_paths"].append(name)
                with patch.object(local, "_snapshot_entry", side_effect=PermissionError(self.secret)):
                    failure = self.assert_prepare_failure(action, "action." + field, "exact_file_unavailable")
                self.assertEqual(failure["detail_sha256"],
                                 hashlib.sha256(b"context input unavailable").hexdigest())

    def test_missing_read_still_rejects_without_disclosing_the_filename(self):
        action = copy.deepcopy(self.action)
        action["read_scopes"].append(self.secret)
        self.assert_prepare_failure(action, "action.read_scopes", "input_missing")

    def test_oversize_exact_files_report_too_large_without_echoing_paths(self):
        name = "0-" + self.secret
        # Above what the runtime can open (local._SOURCE_READ_FILE_BYTES), with or without source_read.
        (self.f.workspace / name).write_bytes(b"x" * (local._SOURCE_READ_FILE_BYTES + 1))
        for field in ("read_scopes", "write_scopes", "target_paths"):
            with self.subTest(field=field):
                action = copy.deepcopy(self.action)
                action[field] = [name]
                if field == "read_scopes":
                    action["command_templates"] = [{"argv": ["cat", name], "effects": ["read"],
                        "targets": [{"operation": "read", "path": name, "argv_index": 1}]}]
                    action["target_paths"] = [name]
                if field == "write_scopes":
                    action["operations"].append("modify")
                    action["target_paths"].append(name)
                failure = self.assert_prepare_failure(action, "action." + field, "exact_file_too_large")
                self.assertEqual(failure["detail_sha256"], hashlib.sha256(
                    b"context input exceeds the whole-file snapshot bound").hexdigest())
        # A directory is still unavailable, not too large.
        (self.f.workspace / "directory").mkdir()
        action = copy.deepcopy(self.action)
        action["read_scopes"] = ["directory"]
        self.assert_prepare_failure(action, "action.read_scopes", "exact_file_unavailable")

    def test_issue_vocabulary_is_known_to_the_live_native_host(self):
        from pathlib import Path
        import importlib.util

        source = Path.home() / ".codex-multi" / "switchboard" / "src" / "nokiy-native-host.py"
        if not source.is_file():
            self.skipTest("no live native host")
        spec = importlib.util.spec_from_file_location("live_native_host_vocabulary", source)
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        self.assertEqual(caller._ACTION_ISSUE_PAIRS - host._ACTION_ISSUE_PAIRS, set())

    def test_unbound_read_becomes_a_typed_source_read(self):
        # V430: an exact read no cat template covers is opened by the typed source reader
        # instead of a command_templates invalid_bindings refusal.
        (self.f.workspace / "other.txt").write_text(self.secret)
        action = copy.deepcopy(self.action)
        action["read_scopes"].append("other.txt")
        adapted, repairs = local.adapt_action(action, self.f.workspace)
        self.assertIs(adapted["source_read"], True)
        self.assertIn("uncovered_reads_to_source_read", {repair["rule"] for repair in repairs})

    def test_typed_and_legacy_unsupported_commands_keep_their_original_codes(self):
        cases = (
            ([{"argv": ["echo", self.secret], "effects": ["read"], "targets": []}], local.CODE),
            (["echo " + self.secret], "NOKIY_FULL_STACK_PREPARATION_FAILED"),
            (["cat input.txt | " + self.secret], "NOKIY_FULL_STACK_PREPARATION_FAILED"),
        )
        for templates, blocker in cases:
            with self.subTest(templates=templates):
                action = copy.deepcopy(self.action)
                action["command_templates"] = templates
                original = copy.deepcopy(action)
                self.assert_prepare_failure(action, "action.command_templates", "unsupported_command", blocker=blocker)
                self.assertEqual(action, original)

    def test_malformed_and_misbound_typed_commands_still_reject(self):
        template = copy.deepcopy(self.action["command_templates"][0])
        for templates, reason in (
            ([{"argv": [self.secret]}], "invalid_shape"),
            ([{**template, "effects": ["modify"]}], "invalid_shape"),
            ([{**template, "targets": []}], "invalid_bindings"),
            ([template, template], "invalid_bindings"),
        ):
            with self.subTest(templates=templates):
                action = copy.deepcopy(self.action)
                action["command_templates"] = templates
                self.assert_prepare_failure(action, "action.command_templates", reason)

    def test_serializer_keeps_only_bounded_static_issue_pairs(self):
        safe = {"path": "action.read_scopes", "reason": "unsafe_path"}
        for code in (local.CODE, "NOKIY_FULL_STACK_PREPARATION_FAILED"):
            failure = caller._failure(caller.EmbeddedNokiyError(code, self.secret, issues=[safe] * 8))
            self.assertEqual(failure["issues"], [safe] * 8)
            self.assertIsNot(failure["issues"][0], safe)
            for issues in (None, [], [safe] * 9, [safe, {**safe, "detail": self.secret}],
                           [{**safe, "path": self.secret}], [{**safe, "reason": self.secret}],
                           [{**safe, "path": "action.read_scopes[0]"}],
                           [{**safe, "reason": "invalid_shape"}], [None]):
                with self.subTest(code=code, issues=issues):
                    failure = caller._failure(caller.EmbeddedNokiyError(code, self.secret, issues=issues))
                    self.assertNotIn("issues", failure)
                    self.assertNotIn(self.secret, json.dumps(failure))
                    self.assertEqual(failure["detail_sha256"], hashlib.sha256(self.secret.encode()).hexdigest())
        unknown = caller._failure(caller.EmbeddedNokiyError("NOKIY_UNKNOWN_FAILURE", self.secret, issues=[safe]))
        self.assertNotIn("issues", unknown)

    def test_valid_context_and_missing_create_target_keep_existing_authority(self):
        for create in (False, True):
            with self.subTest(create=create):
                action = copy.deepcopy(self.action)
                if create:
                    action["operations"].append("create")
                    action["write_scopes"] = ["new.txt"]
                    action["target_paths"].append("new.txt")
                original = copy.deepcopy(action)
                capsule, contract = local.compile_context(self.f.workspace, action)
                local.verify_context(self.f.workspace, capsule, contract)
                self.assertEqual(action, original)
                self.assertEqual(contract["read_scopes"], ["input.txt"])
                self.assertEqual(contract["write_scopes"], action["write_scopes"])
                self.assertEqual(contract["allowed_operations"], sorted(action["operations"]))
                self.assertEqual(contract["command_templates"], action["command_templates"])
                self.assertNotIn("issues", contract)
                self.assertNotIn("issues", capsule)
                if create:
                    self.assertIsNone(capsule["dcf_generation"]["source_snapshot"]["new.txt"])
                    self.assertFalse((self.f.workspace / "new.txt").exists())


if __name__ == "__main__":
    unittest.main()
