"""Real caller preflight for non-DCF preparation and no-downgrade boundaries."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

import test_full_core as fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import local_context as local
from codex_collaboration_harness import source_excerpt as excerpts


class LocalContextTests(unittest.TestCase):
    def test_rejected_file_search_experiment_does_not_widen_exact_reads(self):
        self.action["read_search_scopes"] = ["input.txt"]
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "not an admitted local capability"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_directory_discovery_does_not_require_known_files(self):
        source = self.f.workspace / "src"
        source.mkdir()
        (source / "unknown.txt").write_text("discover me")
        self.action.update(read_scopes=[], target_paths=[], command_templates=[], read_directories=["src"])
        result = self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, capsule, contract = caller._verify_context(request)
        self.assertEqual(contract["read_commands"]["roots"], ["src"])
        self.assertEqual(contract["read_scopes"], ["src/**"])
        self.assertEqual(contract["write_scopes"], [])
        self.assertEqual(capsule["dcf_generation"]["source_snapshot"], {})
        self.assertIn("--no-config", capsule["context_summary"])
        (source / "later.txt").write_text("another valid read")
        caller._verify_context(request)

    def test_directory_discovery_cannot_be_symlink(self):
        source = self.f.workspace / "src"
        source.symlink_to(self.f.root)
        self.action["read_directories"] = ["src"]
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.prepare()

    def test_directory_discovery_rechecks_identity(self):
        source = self.f.workspace / "src"
        source.mkdir()
        self.action["read_directories"] = ["src"]
        self.prepare()
        source.rename(self.f.workspace / "retired")
        source.mkdir()
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "STALE"):
            caller._verify_context(caller.load_request(self.output / "request.json"))

    def setUp(self):
        self.core = fixtures.FullCoreTests()
        self.core.setUp()
        self.addCleanup(self.core.doCleanups)
        self.f = self.core.f
        self.draft = json.loads(self.f.request_path.read_text())
        for k in ("context_capsule", "jspace_contract", "execution_profile"):
            self.draft.pop(k, None)
        self.action = {"mission": {"mission_id": "flex", "task_id": "local", "mode": "DELIVERY",
            "objective": "Read exact input", "current_predicate": "input_read"},
            "context_summary": "Only read the named fixture; no deployment or broker effects.",
            "operations": ["read", "command"], "read_scopes": ["input.txt"], "write_scopes": [],
            "target_paths": ["input.txt"], "command_templates": [{"argv": ["cat", "input.txt"],
                "effects": ["read"], "targets": [{"operation": "read", "path": "input.txt", "argv_index": 1}]}]}
        (self.f.workspace / "input.txt").write_text("bounded input\n")
        self.output = self.f.root / "prepared-local"

    def prepare(self, surface=None):
        self.f.request_path.write_text(json.dumps(self.draft))
        action_path = self.f.root / "action-local.json"
        action_path.write_text(json.dumps(self.action))
        return stack.prepare_request(self.f.request_path, action_path, surface, self.output)

    def _diagnostic_cli(self):
        self.f.request_path.write_text(json.dumps(self.draft))
        action_path = self.f.root / "action-local.json"
        action_path.write_text(json.dumps(self.action))
        output = io.StringIO()
        with patch("sys.stdout", output), patch.object(
                caller, "execute", side_effect=AssertionError("model execution forbidden")) as execute:
            code = caller.main(["prepare", "--request", str(self.f.request_path),
                                "--action", str(action_path), "--output-dir", str(self.output)])
        execute.assert_not_called()
        return code, output.getvalue()

    def test_prepare_cli_reports_static_action_issues_without_echoing_inputs(self):
        secret = "NOKIY_LOCAL_DIAGNOSTIC_SECRET_SENTINEL"
        (self.f.workspace / "input.txt").write_text(secret)
        self.draft["prompt"] = secret
        baseline = copy.deepcopy(self.action)
        cases = (
            ({"read_scopes": ["input.txt", secret + ".txt"]}, "action.read_scopes",
             "input_missing", "read input missing"),
            ({"command_templates": [{"program": secret, "args": [secret]}]},
             "action.command_templates", "invalid_shape",
             "commands require exact typed argv/effects/targets"),
            ({"command_templates": [{"argv": ["python3", secret], "effects": ["read"], "targets": []}]},
             "action.command_templates", "unsupported_command",
             "unsupported command; use file tools or parent native validation"),
        )
        for changes, path, reason, detail in cases:
            with self.subTest(reason=reason):
                self.action = copy.deepcopy(baseline)
                self.action.update(changes)
                code, raw = self._diagnostic_cli()
                result = json.loads(raw)
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "BLOCKED_PREEXECUTION")
                self.assertEqual(result["first_typed_blocker"], local.CODE)
                self.assertEqual(result["detail_sha256"], hashlib.sha256(detail.encode()).hexdigest())
                self.assertEqual(result["issues"], [{"path": path, "reason": reason}])
                self.assertEqual(result["authority_effect"], "none")
                self.assertIs(result["fallback_used"], False)
                self.assertEqual(result["native_codex_control_plane_mutation_count"], 0)
                self.assertNotIn(secret, raw)
                self.assertFalse(self.output.exists())

    def test_template_boundaries_reject_with_structured_metadata(self):
        template = copy.deepcopy(self.action["command_templates"][0])
        cases = (
            (None, "invalid_shape"),
            ([template] * 33, "invalid_shape"),
            ([None], "invalid_shape"),
            ([{**template, "argv": []}], "invalid_shape"),
            ([{**template, "argv": [True]}], "invalid_shape"),
            ([{**template, "effects": ["modify"]}], "invalid_shape"),
            ([{**template, "argv": ["cat", "unscoped.txt"]}], "unsupported_command"),
            ([{**template, "targets": []}], "invalid_bindings"),
            ([template, template], "invalid_bindings"),
        )
        for templates, reason in cases:
            with self.subTest(reason=reason, templates=templates):
                action = copy.deepcopy(self.action)
                action["command_templates"] = templates
                with self.assertRaises(caller.EmbeddedNokiyError) as caught:
                    local.compile_context(self.f.workspace, action)
                self.assertEqual(caught.exception.code, local.CODE)
                self.assertEqual(caught.exception.issues,
                                 [{"path": "action.command_templates", "reason": reason}])

    def test_absent_nonread_targets_keep_their_existing_snapshot_contract(self):
        for operation in ("create", "modify"):
            with self.subTest(operation=operation):
                action = copy.deepcopy(self.action)
                action["operations"].append(operation)
                action["write_scopes"] = ["new.txt"]
                action["target_paths"].append("new.txt")
                capsule, contract = local.compile_context(self.f.workspace, action)
                self.assertIsNone(capsule["dcf_generation"]["source_snapshot"]["new.txt"])
                self.assertEqual(contract["write_scopes"], ["new.txt"])
                self.assertNotIn("new.txt", contract["read_scopes"])
                self.assertFalse((self.f.workspace / "new.txt").exists())

    def test_prepare_cli_success_is_not_mislabeled_as_an_input_issue(self):
        code, raw = self._diagnostic_cli()
        result = json.loads(raw)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "PREPARED")
        self.assertNotIn("issues", result)
        self.assertNotIn("first_typed_blocker", result)
        self.assertTrue((self.output / "request.json").is_file())

    def test_source_read_opt_in_binds_exact_scope_without_cat_template(self):
        self.action.update(source_read=True, command_templates=[])
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        caller.preflight(request)
        _, _, contract = caller._verify_context(request)
        self.assertIs(contract["source_read"], True)
        self.assertEqual(contract["read_scopes"], ["input.txt"])
        self.assertEqual(contract["command_templates"], [])
        self.assertEqual(contract["allowed_operations"], ["command", "read"])

    def test_source_read_prepared_create_readback_binds_scope_digests_and_absence(self):
        (self.f.workspace / "tests").mkdir()
        self.action.update(source_read=True, command_templates=[], read_directories=["tests"],
                           operations=["read", "command", "create"],
                           write_scopes=["tests/new.py"], target_paths=["input.txt", "tests/new.py"])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        caller.preflight(request)
        _, capsule, contract = caller._verify_context(request)
        self.assertEqual(contract["read_scopes"], ["input.txt", "tests/**", "tests/new.py"])
        self.assertEqual(contract["write_scopes"], ["tests/new.py"])
        self.assertEqual(contract["declared_targets"], ["input.txt", "tests/new.py"])
        self.assertEqual(contract["allowed_operations"], ["command", "create", "read"])
        self.assertEqual(self.action["read_scopes"], ["input.txt"])
        self.assertIsNone(capsule["dcf_generation"]["source_snapshot"]["tests/new.py"])
        self.assertEqual(contract["dcf_generation"], capsule["dcf_generation"])
        self.assertNotIn("tests/new.py", [ref["id"] for ref in capsule["evidence_refs"]])
        authorization = {key: contract[key] for key in (
            "repo_root", "matched_surface_ids", "declared_targets", "read_scopes", "write_scopes",
            "allowed_operations", "denied_operations", "command_templates", "command_effect_policy",
            "expansion", "source_read", "read_commands")}
        authorization.update(schema_version="jspace_authorization_v1", required_domain_bindings={})
        self.assertEqual(contract["authorization_semantic_sha256"], caller._canonical_sha256(authorization))
        self.assertEqual(capsule["jspace_semantic_sha256"], contract["authorization_semantic_sha256"])
        without_exact_scope = copy.deepcopy(authorization)
        without_exact_scope["read_scopes"].remove("tests/new.py")
        self.assertNotEqual(contract["authorization_semantic_sha256"],
                            caller._canonical_sha256(without_exact_scope))
        self.assertEqual(contract["content_sha256"], caller._canonical_sha256(
            {key: value for key, value in contract.items() if key != "content_sha256"}))
        self.assertEqual(capsule["semantic_sha256"], caller._canonical_sha256(
            {key: value for key, value in capsule.items() if key != "semantic_sha256"}))

    def test_source_read_create_readback_does_not_add_unearned_exact_scopes(self):
        (self.f.workspace / "tests").mkdir()
        (self.f.workspace / "tests-sibling").mkdir()
        (self.f.workspace / "tests" / "existing.py").write_text("existing\n")
        self.action.update(read_directories=["tests"], operations=["read", "command", "create"],
                           write_scopes=["tests/new.py"], target_paths=["input.txt", "tests/new.py"])
        cases = (
            ("directory prefix sibling", True, {
                "write_scopes": ["tests-sibling/new.py"],
                "target_paths": ["input.txt", "tests-sibling/new.py"]}),
            ("outside admitted root", True, {
                "write_scopes": ["outside.py"], "target_paths": ["input.txt", "outside.py"]}),
            ("no source_read", False, {}),
            ("no create", True, {"operations": ["read", "command", "modify"]}),
            ("no directory grant", True, {"read_directories": []}),
            ("not declared", True, {"write_scopes": ["input.txt"], "target_paths": ["input.txt"]}),
            ("not write-granted", True, {"write_scopes": ["input.txt"]}),
            ("existing create target", True, {
                "write_scopes": ["tests/existing.py"],
                "target_paths": ["input.txt", "tests/existing.py"]}),
            ("existing modify target", True, {
                "operations": ["read", "command", "modify"], "write_scopes": ["tests/existing.py"],
                "target_paths": ["input.txt", "tests/existing.py"]}),
            ("whitespace alias", True, {
                "write_scopes": ["tests/new.py "], "target_paths": ["input.txt", "tests/new.py "]}),
            ("backslash alias", True, {
                "write_scopes": ["tests/a\\b.py"], "target_paths": ["input.txt", "tests/a\\b.py"]}),
            ("hidden target", True, {
                "write_scopes": ["tests/.new.py"], "target_paths": ["input.txt", "tests/.new.py"]}),
        )
        for reason, source_read, changes in cases:
            with self.subTest(reason=reason):
                action = copy.deepcopy(self.action)
                action.update(changes)
                if source_read:
                    action["source_read"] = True
                _, contract = local.compile_context(self.f.workspace, action)
                self.assertEqual(contract["read_scopes"],
                                 sorted(["input.txt"] + [root + "/**" for root in action["read_directories"]]))
                self.assertEqual(contract["write_scopes"], sorted(action["write_scopes"]))
                self.assertEqual(contract["allowed_operations"], sorted(action["operations"]))

    def test_source_read_create_readback_preserves_missing_read_and_scope_rejections(self):
        (self.f.workspace / "tests").mkdir()
        self.action.update(source_read=True, read_directories=["tests"],
                           operations=["read", "command", "create"],
                           write_scopes=["tests/new.py"], target_paths=["input.txt", "tests/new.py"])
        cases = (
            ({"read_scopes": ["input.txt", "tests/new.py"]}, "read input missing"),
            ({"target_paths": ["input.txt"]}, "explicit scopes and operations disagree"),
            ({"write_scopes": ["../outside.py"], "target_paths": ["input.txt", "../outside.py"]},
             "exact workspace-relative files"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(caller.EmbeddedNokiyError, message):
                action = copy.deepcopy(self.action)
                action.update(changes)
                local.compile_context(self.f.workspace, action)

    def test_source_read_create_readback_rejects_precreation(self):
        (self.f.workspace / "tests").mkdir()
        self.action.update(source_read=True, read_directories=["tests"],
                           operations=["read", "command", "create"],
                           write_scopes=["tests/new.py"], target_paths=["input.txt", "tests/new.py"])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        (self.f.workspace / "tests" / "new.py").write_text("concurrent owner\n")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_STALE"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_source_read_create_readback_rejects_directory_identity_drift(self):
        directory = self.f.workspace / "tests"
        directory.mkdir()
        self.action.update(source_read=True, read_directories=["tests"],
                           operations=["read", "command", "create"],
                           write_scopes=["tests/new.py"], target_paths=["input.txt", "tests/new.py"])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        directory.rename(self.f.workspace / "retired-tests")
        directory.mkdir()
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "discovery directory identity changed"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_source_read_create_readback_scope_tamper_rejected_by_preflight(self):
        (self.f.workspace / "tests").mkdir()
        self.action.update(source_read=True, read_directories=["tests"],
                           operations=["read", "command", "create"],
                           write_scopes=["tests/new.py"], target_paths=["input.txt", "tests/new.py"])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        wire = request.to_wire(include_identity=False)
        contract_path = Path(wire["jspace_contract"]["path"])
        contract = json.loads(contract_path.read_text())
        contract["read_scopes"].remove("tests/new.py")
        contract["content_sha256"] = caller._canonical_sha256(
            {key: value for key, value in contract.items() if key != "content_sha256"})
        contract_path.write_text(json.dumps(contract))
        wire["jspace_contract"]["sha256"] = caller._file_sha256(contract_path)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "semantic digest differs"):
            caller.preflight(caller.decode_request(wire))

    def test_source_read_requires_explicit_true_and_read_command_scope(self):
        self.action["command_templates"] = []
        for value in (False, "true", 1, None):
            with self.subTest(value=value), self.assertRaises(caller.EmbeddedNokiyError):
                action = copy.deepcopy(self.action)
                action["source_read"] = value
                local.compile_context(self.f.workspace, action)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "explicit templates"):
            local.compile_context(self.f.workspace, self.action)
        for operations, reads in ((["read"], ["input.txt"]),
                                  (["command"], ["input.txt"]),
                                  (["read", "command"], [])):
            with self.subTest(operations=operations, reads=reads), self.assertRaisesRegex(
                    caller.EmbeddedNokiyError, "source_read requires exact files"):
                action = copy.deepcopy(self.action)
                action.update(source_read=True, operations=operations, read_scopes=reads)
                local.compile_context(self.f.workspace, action)

    def test_source_read_rejects_scope_aliases_before_authorization(self):
        (self.f.workspace / " secret.txt").write_text("literal")
        (self.f.workspace / "secret.txt").write_text("other")
        (self.f.workspace / "a\\b").write_text("literal")
        (self.f.workspace / "a").mkdir()
        (self.f.workspace / "a" / "b").write_text("other")
        for name in (" secret.txt", "secret.txt ", "a\\b"):
            with self.subTest(name=name), self.assertRaisesRegex(
                    caller.EmbeddedNokiyError, "canonical exact file names"):
                action = copy.deepcopy(self.action)
                action.update(source_read=True, read_scopes=[name], target_paths=[name],
                              command_templates=[])
                local.compile_context(self.f.workspace, action)

    def test_snapshot_rejects_symlink_swap_after_path_validation(self):
        source = self.f.workspace / "input.txt"
        other = self.f.root / "outside.txt"
        other.write_text("outside")
        original = local._file

        def swap(workspace, name):
            path = original(workspace, name)
            source.unlink()
            source.symlink_to(other)
            return path

        with patch.object(local, "_file", side_effect=swap), self.assertRaises(
                caller.EmbeddedNokiyError):
            local._snapshot(self.f.workspace, ["input.txt"], max_bytes=local._SOURCE_READ_FILE_BYTES)

    def test_source_read_allows_bounded_file_without_inlining_it(self):
        source = self.f.workspace / "input.txt"
        source.write_bytes(b"x" * (caller.MAX_REQUEST_BYTES + 1))
        self.action.update(source_read=True, command_templates=[])
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, capsule, contract = caller._verify_context(request)
        self.assertLess(len(capsule["context_summary"]), local._MAX_SUMMARY_CHARS)
        self.assertIs(contract["source_read"], True)
        source.write_bytes(b"x" * (local._SOURCE_READ_FILE_BYTES + 1))
        with self.assertRaises(caller.EmbeddedNokiyError):
            caller.preflight(request)

    def test_source_read_tamper_does_not_survive_caller_preflight(self):
        self.action.update(source_read=True, command_templates=[])
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        wire = request.to_wire(include_identity=False)
        contract_path = Path(wire["jspace_contract"]["path"])
        contract = json.loads(contract_path.read_text())
        contract.pop("source_read")
        contract["content_sha256"] = caller._canonical_sha256(
            {key: value for key, value in contract.items() if key != "content_sha256"})
        contract_path.write_text(json.dumps(contract))
        wire["jspace_contract"]["sha256"] = caller._file_sha256(contract_path)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "semantic digest differs"):
            caller.preflight(caller.decode_request(wire))

    def verifier_command(self):
        entry = (self.f.workspace / "verify_local.py").resolve()
        entry.write_text("print('verified')\n")
        self.action["read_scopes"].append("verify_local.py")
        self.action["target_paths"].append("verify_local.py")
        self.action["command_templates"].append({"argv": ["cat", "verify_local.py"],
            "effects": ["read"], "targets": [{"operation": "read", "path": "verify_local.py", "argv_index": 1}]})
        executable = Path(sys.executable).resolve()
        (self.f.artifacts / "verifier-local").mkdir()
        command = {"argv": [str(executable), str(entry)],
                   "executable_sha256": caller._file_sha256(executable),
                   "pinned_files": [{"path": str(entry), "sha256": caller._file_sha256(entry)}],
                   "timeout_seconds": 30,
                   "scratch_root": str(self.f.artifacts / "verifier-local"), "network": False}
        self.action["verifier_commands"] = [command]
        return command, entry

    def verifier_import_command(self, names=("src", "lib")):
        command, _ = self.verifier_command()
        executable = self.f.root / "python3"
        shutil.copyfile(command["argv"][0], executable)
        executable.chmod(0o700)
        command["argv"][0] = str(executable)
        command["executable_sha256"] = caller._file_sha256(executable)
        roots = []
        for name in names:
            root = self.f.workspace / name
            source = root / "package" / "admitted.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n")
            self.action["read_scopes"].append(source.relative_to(self.f.workspace).as_posix())
            roots.append(str(root))
        self.action.update(source_read=True, command_templates=[])
        command["python_import_roots"] = roots
        return command, roots

    def source_sections(self):
        source = self.f.workspace / "guard.py"
        source.write_text("def first():\n    return 1\n\ndef second():\n    return 2\n")
        self.action["read_scopes"].append("guard.py")
        self.action["target_paths"].append("guard.py")
        self.action["command_templates"].append({"argv": ["cat", "guard.py"],
            "effects": ["read"], "targets": [{"operation": "read", "path": "guard.py", "argv_index": 1}]})
        self.action["source_sections"] = [
            {"path": "guard.py", "start_line": 1, "end_line": 2},
            {"path": "guard.py", "start_line": 4, "end_line": 5},
        ]
        return source

    def test_source_sections_are_exact_context_without_new_grants(self):
        source = self.source_sections()
        plain_action = copy.deepcopy(self.action)
        plain_action.pop("source_sections")
        plain = local.compile_context(self.f.workspace, plain_action)[1]
        capsule, contract = local.compile_context(self.f.workspace, self.action)
        self.assertEqual(contract["authorization_semantic_sha256"],
                         plain["authorization_semantic_sha256"])
        self.assertEqual(contract["read_scopes"], plain["read_scopes"])
        self.assertEqual(contract["write_scopes"], plain["write_scopes"])
        sections = json.loads(capsule["context_summary"].split(local._SECTIONS_MARKER)[1])
        self.assertEqual([part["text"] for part in sections],
                         ["def first():\n    return 1\n", "def second():\n    return 2\n"])
        self.assertEqual(sections[0]["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(capsule["dcf_generation"]["source_sections"][0]["section_sha256"],
                         hashlib.sha256(sections[0]["text"].encode()).hexdigest())
        self.assertNotIn("source_sections", contract)
        self.prepare()
        caller.preflight(caller.load_request(self.output / "request.json"))

    def text_source_sections(self):
        sources = {
            "guard.rs": '// header\r\nconst MESSAGE: &str = "café";\r\n// outside\r\n',
            "guard.js": '// header\nexport const message = "café";\n// outside\n',
            "guard.json": '{\n  "message": "café"\n}\n',
            "guard.md": "# Header\nRésumé **text**\nOutside range\n",
        }
        self.action["source_sections"] = []
        for name, text in sources.items():
            (self.f.workspace / name).write_bytes(text.encode("utf-8"))
            self.action["read_scopes"].append(name)
            self.action["target_paths"].append(name)
            self.action["command_templates"].append({"argv": ["cat", name],
                "effects": ["read"], "targets": [{"operation": "read", "path": name, "argv_index": 1}]})
            self.action["source_sections"].append({"path": name, "start_line": 2, "end_line": 2})
        return sources

    def test_text_source_sections_are_exact_context_without_new_grants(self):
        sources = self.text_source_sections()
        plain_action = copy.deepcopy(self.action)
        plain_action.pop("source_sections")
        _, plain = local.compile_context(self.f.workspace, plain_action)
        capsule, contract = local.compile_context(self.f.workspace, self.action)
        for field in ("authorization_semantic_sha256", "allowed_operations",
                      "read_scopes", "write_scopes", "command_templates"):
            self.assertEqual(contract[field], plain[field])
        expected = []
        for name, text in sources.items():
            data = text.encode("utf-8")
            excerpt = data.splitlines(keepends=True)[1]
            expected.append({"path": name, "start_line": 2, "end_line": 2,
                             "source_sha256": hashlib.sha256(data).hexdigest(),
                             "section_sha256": hashlib.sha256(excerpt).hexdigest(),
                             "text": excerpt.decode("utf-8")})
        sections = json.loads(capsule["context_summary"].split(local._SECTIONS_MARKER)[1])
        self.assertEqual(sections, expected)
        self.assertEqual(capsule["dcf_generation"]["source_sections"],
                         [{key: value for key, value in part.items() if key != "text"} for part in expected])
        self.assertNotIn("source_sections", contract)
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        caller.preflight(request)
        _, prepared, _ = caller._verify_context(request)
        self.assertEqual(json.loads(prepared["context_summary"].split(local._SECTIONS_MARKER)[1]),
                         expected)

    def test_exact_reader_keeps_python_default_for_text_sources(self):
        sources = self.text_source_sections()
        for name, text in sources.items():
            with self.subTest(name=name):
                with self.assertRaises(caller.EmbeddedNokiyError):
                    excerpts._read_exact(self.f.workspace, name)
                self.assertEqual(excerpts._read_exact(self.f.workspace, name, python_only=False),
                                 text.encode("utf-8"))
        source = self.source_sections()
        self.assertEqual(excerpts._read_exact(self.f.workspace, "guard.py"), source.read_bytes())

    def test_text_source_sections_reject_ungranted_and_aliased_paths(self):
        self.text_source_sections()
        (self.f.workspace / "ungranted.rs").write_text("fn main() {}\n", encoding="utf-8")
        names = ("ungranted.rs", "./guard.rs", "nested/../guard.rs",
                 "guard*.rs", ".git/guard.rs", str(self.f.workspace / "guard.rs"))
        for name in names:
            with self.subTest(name=name):
                action = copy.deepcopy(self.action)
                action["source_sections"] = [{"path": name, "start_line": 1, "end_line": 1}]
                if name != "ungranted.rs":
                    action.update(read_scopes=[name], target_paths=[name], command_templates=[])
                with self.assertRaises(caller.EmbeddedNokiyError):
                    local.compile_context(self.f.workspace, action)
                if name != "ungranted.rs":
                    with self.assertRaises(caller.EmbeddedNokiyError):
                        excerpts._read_exact(self.f.workspace, name, python_only=False)

    def test_text_source_sections_reject_symlink_paths(self):
        self.text_source_sections()
        (self.f.workspace / "linked.rs").symlink_to("guard.rs")
        (self.f.workspace / "linked-dir").symlink_to(".", target_is_directory=True)
        for name in ("linked.rs", "linked-dir/guard.rs"):
            with self.subTest(name=name):
                action = copy.deepcopy(self.action)
                action.update(read_scopes=[name], target_paths=[name], command_templates=[],
                              source_sections=[{"path": name, "start_line": 2, "end_line": 2}])
                with self.assertRaises(caller.EmbeddedNokiyError):
                    local.compile_context(self.f.workspace, action)
                with self.assertRaises(caller.EmbeddedNokiyError):
                    excerpts._read_exact(self.f.workspace, name, python_only=False)

    def test_text_source_sections_reject_invalid_utf8_and_out_of_range(self):
        self.text_source_sections()
        for data in (b"// header\n\x00\xff\n", b"// only one line\n"):
            with self.subTest(data=data):
                (self.f.workspace / "guard.rs").write_bytes(data)
                with self.assertRaises(caller.EmbeddedNokiyError):
                    local.compile_context(self.f.workspace, self.action)

    def test_text_source_sections_reject_stale_and_tampered_context(self):
        self.text_source_sections()
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, capsule, contract = caller._verify_context(request)
        prefix, raw = capsule["context_summary"].split(local._SECTIONS_MARKER)
        sections = json.loads(raw)
        sections[0]["text"] = "forged\n"
        sections[0]["section_sha256"] = hashlib.sha256(b"forged\n").hexdigest()
        changed = copy.deepcopy(capsule)
        changed["context_summary"] = prefix + local._SECTIONS_MARKER + json.dumps(sections)
        changed["dcf_generation"]["source_sections"][0]["section_sha256"] = sections[0]["section_sha256"]
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.verify_context(self.f.workspace, changed, contract)
        for field in ("section_sha256", "source_sha256"):
            with self.subTest(field=field):
                changed = copy.deepcopy(capsule)
                changed["dcf_generation"]["source_sections"][0][field] = "0" * 64
                with self.assertRaises(caller.EmbeddedNokiyError):
                    local.verify_context(self.f.workspace, changed, contract)
        source = self.f.workspace / "guard.rs"
        source.write_bytes(source.read_bytes() + b"// changed outside ranges\n")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_STALE"):
            caller.preflight(request)

    def test_source_sections_preserve_existing_write_scope(self):
        self.source_sections()
        self.action.update(operations=["read", "command", "modify"], write_scopes=["input.txt"])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, _, contract = caller._verify_context(request)
        self.assertEqual(contract["write_scopes"], ["input.txt"])

    def test_source_sections_allow_scoped_patch_without_whole_file_cat(self):
        self.source_sections()
        self.action.update(operations=["read", "modify"], read_scopes=["guard.py"],
                           write_scopes=["guard.py"], target_paths=["guard.py"],
                           command_templates=[])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, capsule, contract = caller._verify_context(request)
        self.assertEqual(contract["command_templates"], [])
        self.assertEqual(contract["allowed_operations"], ["modify", "read"])
        self.assertIn("def first():", capsule["context_summary"])

    def test_large_source_only_projects_bounded_verified_sections(self):
        source = self.source_sections()
        source.write_text("".join(f"line {line:04d}\n" for line in range(1, 2903)))
        self.action.update(operations=["read", "modify"], read_scopes=["guard.py"],
                           write_scopes=["guard.py"], target_paths=["guard.py"],
                           command_templates=[], source_sections=[
                               {"path": "guard.py", "start_line": start, "end_line": start + 23}
                               for start in (1, 1001, 1901, 2801)])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, capsule, contract = caller._verify_context(request)
        sections = json.loads(capsule["context_summary"].split(local._SECTIONS_MARKER)[1])
        self.assertEqual(len(sections), 4)
        self.assertLess(len(capsule["context_summary"]), 12_000)
        self.assertNotIn("line 1500", capsule["context_summary"])
        self.assertEqual(contract["command_templates"], [])

    def test_partial_source_sections_do_not_cover_other_exact_reads(self):
        self.source_sections()
        self.action.update(operations=["read"], command_templates=[])
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "no admitted read command"):
            self.prepare()

    def test_source_sections_reject_unbound_and_unbounded_ranges(self):
        source = self.source_sections()
        source.write_text("".join(f"line {line}\n" for line in range(1, 132)))
        invalid = [
            [{"path": "./input.txt", "start_line": 1, "end_line": 1}],
            [{"path": "other.py", "start_line": 1, "end_line": 1}],
            [{"path": "guard.py", "start_line": 0, "end_line": 1}],
            [{"path": "guard.py", "start_line": True, "end_line": 1}],
            [{"path": "guard.py", "start_line": 1, "end_line": 121}],
            [{"path": "guard.py", "start_line": 132, "end_line": 132}],
            [{"path": "guard.py", "start_line": 1, "end_line": 1, "extra": True}],
            [{"path": "guard.py", "start_line": 1, "end_line": 1}] * 2,
            [{"path": "guard.py", "start_line": line, "end_line": line} for line in range(1, 6)],
        ]
        for sections in invalid:
            with self.subTest(sections=sections), self.assertRaises(caller.EmbeddedNokiyError):
                action = copy.deepcopy(self.action)
                action["source_sections"] = sections
                local.compile_context(self.f.workspace, action)

    def test_source_sections_reject_per_section_and_total_bytes(self):
        source = self.source_sections()
        source.write_text("".join("x" * 3000 + "\n" for _ in range(4)))
        action = copy.deepcopy(self.action)
        action["source_sections"] = [{"path": "guard.py", "start_line": 1, "end_line": 3}]
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.compile_context(self.f.workspace, action)
        action["source_sections"] = [{"path": "guard.py", "start_line": line, "end_line": line}
                                     for line in range(1, 5)]
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.compile_context(self.f.workspace, action)

    def test_source_sections_reject_existing_summary_budget_overflow(self):
        self.source_sections()
        self.action["context_summary"] = "x" * 11_900
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "context_summary exceeds existing budget"):
            local.compile_context(self.f.workspace, self.action)

    def test_source_sections_full_file_drift_rejected_outside_range(self):
        source = self.source_sections()
        self.prepare()
        source.write_text(source.read_text() + "# changed outside both sections\n")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_STALE"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_source_sections_context_tampering_rejected(self):
        self.source_sections()
        capsule, contract = local.compile_context(self.f.workspace, self.action)
        for summary in (capsule["context_summary"].replace("return 1", "return 9"),
                        capsule["context_summary"].split(local._SECTIONS_MARKER)[0]):
            with self.subTest(summary=summary), self.assertRaisesRegex(
                    caller.EmbeddedNokiyError, "LOCAL_CONTEXT_INVALID"):
                changed = copy.deepcopy(capsule)
                changed["context_summary"] = summary
                local.verify_context(self.f.workspace, changed, contract)

    def test_source_sections_tampering_rejected_by_real_preflight(self):
        self.source_sections()
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        wire = request.to_wire(include_identity=False)
        capsule_path = Path(wire["context_capsule"]["path"])
        capsule = json.loads(capsule_path.read_text())
        capsule["context_summary"] = capsule["context_summary"].replace("return 1", "return 9")
        capsule["semantic_sha256"] = caller._canonical_sha256(
            {key: value for key, value in capsule.items() if key != "semantic_sha256"})
        capsule_path.write_text(json.dumps(capsule))
        wire["context_capsule"]["sha256"] = caller._file_sha256(capsule_path)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_INVALID"):
            caller.preflight(caller.decode_request(wire))

    def test_prepare_without_dcf_or_project_python(self):
        with patch.object(caller, "execute") as execute:
            result = self.prepare()
        execute.assert_not_called()
        self.assertEqual(result["context_mode"], local.MODE)
        self.assertEqual(result["execution_profile"], "direct")
        ready, capsule, contract = caller._verify_context(caller.load_request(self.output / "request.json"))
        self.assertEqual(ready["dcf_generation_id"], "")
        self.assertFalse(capsule["dcf_generation"]["dcf_available"])
        self.assertEqual(contract["matched_surface_ids"], [])
        self.assertEqual(contract["command_effect_policy"], "trusted_argv_effects_v1")
        self.assertNotIn("verifier_commands", contract)
        self.assertNotIn("verifier_artifact_root", contract)
        self.assertNotIn("network", contract["denied_operations"])

    def test_absent_verifier_grant_ignores_artifact_root_for_authorization(self):
        plain = local.compile_context(self.f.workspace, self.action)[1]
        with_root = local.compile_context(self.f.workspace, self.action,
                                          artifact_root=self.f.artifacts)[1]
        self.assertEqual(plain["authorization_semantic_sha256"],
                         with_root["authorization_semantic_sha256"])

    def test_verifier_scratch_must_exist_before_preparation(self):
        command, _ = self.verifier_command()
        Path(command["scratch_root"]).rmdir()
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "allocated before preparation"):
            local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)

    def test_typed_verifier_grant_is_bound_and_revalidated(self):
        plain = local.compile_context(self.f.workspace, self.action)[1]
        command, _ = self.verifier_command()
        _, frozen = local.compile_context(self.f.workspace, self.action,
                                          artifact_root=self.f.artifacts)
        original_argv = list(frozen["verifier_commands"][0]["argv"])
        command["argv"].append("temporary")
        self.assertEqual(frozen["verifier_commands"][0]["argv"], original_argv)
        command["argv"].pop()
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        ready = caller.preflight(request)
        self.assertEqual(ready["status"], "BLOCKED")
        self.assertEqual(ready["first_typed_blocker"], "NOKIY_FULL_CORE_VERIFIER_PARENT_CHANNEL_REQUIRED")
        from codex_collaboration_harness import full_core
        with patch.object(caller, "_run_process", side_effect=AssertionError("must not start a model")):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "PARENT_CHANNEL_REQUIRED"):
                full_core.execute_full_core(request)
        self.assertFalse((request.artifact_root / request.request_id).exists())
        _, capsule, contract = caller._verify_context(request)
        self.assertEqual(contract["verifier_commands"], [command])
        self.assertEqual(contract["verifier_artifact_root"], str(request.artifact_root))
        self.assertIn("network", contract["denied_operations"])
        self.assertNotEqual(contract["authorization_semantic_sha256"], plain["authorization_semantic_sha256"])
        self.assertEqual(capsule["jspace_semantic_sha256"], contract["authorization_semantic_sha256"])

    def test_verifier_import_roots_accept_one_to_four_and_revalidate(self):
        command, roots = self.verifier_import_command(("src", "lib", "third", "fourth"))
        for count in range(1, 5):
            with self.subTest(count=count):
                command["python_import_roots"] = roots[:count]
                capsule, contract = local.compile_context(self.f.workspace, self.action,
                                                          artifact_root=self.f.artifacts)
                self.assertEqual(contract["verifier_commands"][0]["python_import_roots"], roots[:count])
                local.verify_context(self.f.workspace, capsule, contract, artifact_root=self.f.artifacts)

    def test_verifier_import_root_can_contain_directory_read_scope(self):
        _, roots = self.verifier_import_command(("src",))
        self.action["read_scopes"].remove("src/package/admitted.py")
        for directory in ("src", "src/package"):
            with self.subTest(directory=directory):
                self.action["read_directories"] = [directory]
                capsule, contract = local.compile_context(self.f.workspace, self.action,
                                                          artifact_root=self.f.artifacts)
                self.assertEqual(contract["verifier_commands"][0]["python_import_roots"], roots)
                local.verify_context(self.f.workspace, capsule, contract, artifact_root=self.f.artifacts)

    def test_verifier_import_roots_are_copied_and_bound_without_changing_old_grants(self):
        command, roots = self.verifier_import_command()
        old_action = copy.deepcopy(self.action)
        old_action["verifier_commands"][0].pop("python_import_roots")
        _, old = local.compile_context(self.f.workspace, old_action, artifact_root=self.f.artifacts)
        capsule, contract = local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)
        self.assertEqual(old["verifier_commands"], old_action["verifier_commands"])
        self.assertNotIn("python_import_roots", old["verifier_commands"][0])
        self.assertNotEqual(contract["authorization_semantic_sha256"], old["authorization_semantic_sha256"])
        authorization = {key: contract[key] for key in (
            "repo_root", "matched_surface_ids", "declared_targets", "read_scopes", "write_scopes",
            "allowed_operations", "denied_operations", "command_templates", "command_effect_policy",
            "expansion", "source_read", "verifier_artifact_root", "verifier_commands")}
        authorization.update(schema_version="jspace_authorization_v1", required_domain_bindings={})
        self.assertEqual(contract["authorization_semantic_sha256"], caller._canonical_sha256(authorization))
        self.assertEqual(capsule["jspace_semantic_sha256"], contract["authorization_semantic_sha256"])
        bound_roots = list(roots)
        command["python_import_roots"].reverse()
        self.assertEqual(contract["verifier_commands"][0]["python_import_roots"], bound_roots)

    def test_verifier_import_root_tampering_does_not_survive_preflight(self):
        _, roots = self.verifier_import_command()
        self.prepare()
        wire = caller.load_request(self.output / "request.json").to_wire(include_identity=False)
        contract_path = Path(wire["jspace_contract"]["path"])
        original = json.loads(contract_path.read_text())
        for changed_roots in (None, roots[::-1], roots[:1]):
            with self.subTest(roots=changed_roots):
                contract = copy.deepcopy(original)
                command = contract["verifier_commands"][0]
                if changed_roots is None:
                    command.pop("python_import_roots")
                else:
                    command["python_import_roots"] = changed_roots
                contract["content_sha256"] = caller._canonical_sha256(
                    {key: value for key, value in contract.items() if key != "content_sha256"})
                contract_path.write_text(json.dumps(contract))
                wire["jspace_contract"]["sha256"] = caller._file_sha256(contract_path)
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "semantic digest differs"):
                    caller.preflight(caller.decode_request(wire))

    def test_verifier_import_roots_reject_malformed_paths_and_lists(self):
        command, roots = self.verifier_import_command(("src", "lib", "third", "fourth", "fifth"))
        for name in ("unread", ".hidden", "src/.hidden", "src:alternate"):
            directory = self.f.workspace / name
            directory.mkdir()
            if name != "unread":
                source = directory / "admitted.py"
                source.write_text("VALUE = 1\n")
                self.action["read_scopes"].append(source.relative_to(self.f.workspace).as_posix())
        command["python_import_roots"] = roots[:4]
        local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)
        link = self.f.workspace / "linked-src"
        link.symlink_to(roots[0], target_is_directory=True)
        invalid = [None, False, {}, roots[0], [], [1], [""], ["src"],
                   [str(self.f.workspace)], [str(self.f.root)],
                   [str(self.f.workspace / "missing")], [str(self.f.workspace / "unread")],
                   [str(self.f.workspace / ".hidden")], [str(self.f.workspace / "src/.hidden")],
                   [str(self.f.workspace / "src:alternate")], [roots[0] + ":" + roots[1]],
                   [roots[0] + "/package/admitted.py"], [str(link)], [str(link / "package")],
                   [roots[0] + "/"], [roots[0] + "/./package"], [roots[0] + "/../lib"],
                   [roots[0].replace("/src", "//src")], ["/" + roots[0]],
                   [roots[0] + "\x00"], [roots[0] + "\n"], [roots[0], roots[0]], roots]
        for value in invalid:
            with self.subTest(roots=value):
                command["python_import_roots"] = value
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "python_import_roots"):
                    local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)
        command["python_import_roots"] = roots[:1]
        command["python_import_root"] = roots[:1]
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "verifier command fields differ"):
            local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)

    def test_verifier_import_root_requires_existing_contained_read_scope(self):
        command, roots = self.verifier_import_command(("src",))
        for scope in ("src/missing.py", "src/package/missing/**", "input.txt", "src-other/admitted.py"):
            with self.subTest(scope=scope), self.assertRaisesRegex(
                    caller.EmbeddedNokiyError, "existing admitted read scope"):
                local._verifier_commands([command], self.f.workspace, self.f.artifacts,
                                         ["verify_local.py", scope])
        self.assertEqual(command["python_import_roots"], roots)

    def test_verifier_import_roots_are_rechecked_for_missing_and_symlink_directories(self):
        command, roots = self.verifier_import_command(("src",))
        contexts = []
        for value in (roots, [roots[0] + "/package"]):
            command["python_import_roots"] = value
            contexts.append(local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts))
        root = Path(roots[0])
        retired = self.f.workspace / "retired-src"
        root.rename(retired)
        for linked in (False, True):
            if linked:
                root.symlink_to(retired, target_is_directory=True)
            for capsule, contract in contexts:
                with self.subTest(linked=linked, roots=contract["verifier_commands"][0]["python_import_roots"]):
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "python_import_roots"):
                        local.verify_context(self.f.workspace, capsule, contract, artifact_root=self.f.artifacts)

    def test_verifier_import_roots_require_cpython_and_environment_enabled_argv(self):
        command, _ = self.verifier_import_command(("src",))
        original_argv = list(command["argv"])
        original_python = original_argv[0]
        for name in ("python", "python3", "python3.13", "pypy3", "node", "Python", "python3-fake"):
            executable = self.f.root / name
            if str(executable) != original_python:
                shutil.copyfile(original_python, executable)
                executable.chmod(0o700)
            command["argv"][0] = str(executable)
            command["executable_sha256"] = caller._file_sha256(executable)
            if name in {"python", "python3", "python3.13"}:
                local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)
            else:
                with self.subTest(name=name), self.assertRaisesRegex(caller.EmbeddedNokiyError, "CPython"):
                    local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)
                old = copy.deepcopy(self.action)
                old["verifier_commands"][0].pop("python_import_roots")
                local.compile_context(self.f.workspace, old, artifact_root=self.f.artifacts)
        command["executable_sha256"] = caller._file_sha256(Path(original_python))
        for flag in ("-I", "-E", "-IE", "-BI", "-sE"):
            with self.subTest(flag=flag):
                command["argv"] = [original_python, flag, original_argv[1]]
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "without -I/-E"):
                    local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)
                old = copy.deepcopy(self.action)
                old["verifier_commands"][0].pop("python_import_roots")
                local.compile_context(self.f.workspace, old, artifact_root=self.f.artifacts)

    def test_typed_verifier_hash_drift_is_stale(self):
        _, entry = self.verifier_command()
        self.prepare()
        entry.write_text("print('changed')\n")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_STALE"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_typed_verifier_executable_hash_drift_is_stale(self):
        command, entry = self.verifier_command()
        executable = self.f.root / "run-verifier"
        shutil.copyfile("/bin/echo", executable)
        executable.chmod(0o700)
        command["argv"] = [str(executable), str(entry)]
        command["executable_sha256"] = caller._file_sha256(executable)
        self.prepare()
        executable.write_bytes(executable.read_bytes() + b"changed")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_STALE"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_typed_verifier_rejects_other_artifact_root(self):
        self.verifier_command()
        self.prepare()
        other = self.f.root / "other-artifacts"
        other.mkdir()
        wire = caller.load_request(self.output / "request.json").to_wire(include_identity=False)
        wire["artifact_root"] = str(other)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_INVALID"):
            caller.preflight(caller.decode_request(wire))

    def test_typed_verifier_rejects_shell_metachar_and_inline_code(self):
        command, entry = self.verifier_command()
        for argv in (["/bin/sh", str(entry)], ["/usr/bin/env", "sh", str(entry)],
                     [command["argv"][0], str(entry), ";"],
                     [command["argv"][0], "-c", "print(1)", str(entry)]):
            with self.subTest(argv=argv):
                action = copy.deepcopy(self.action)
                action["verifier_commands"][0]["argv"] = argv
                with self.assertRaises(caller.EmbeddedNokiyError):
                    local.compile_context(self.f.workspace, action, artifact_root=self.f.artifacts)

    def test_typed_verifier_rejects_symlink_paths(self):
        command, entry = self.verifier_command()
        executable_link = self.f.root / "linked-python"
        executable_link.symlink_to(command["argv"][0])
        action = copy.deepcopy(self.action)
        action["verifier_commands"][0]["argv"][0] = str(executable_link)
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.compile_context(self.f.workspace, action, artifact_root=self.f.artifacts)
        link = self.f.root / "linked-verify.py"
        link.symlink_to(entry)
        action = copy.deepcopy(self.action)
        action["verifier_commands"][0]["argv"][1] = str(link)
        action["verifier_commands"][0]["pinned_files"][0]["path"] = str(link)
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.compile_context(self.f.workspace, action, artifact_root=self.f.artifacts)
        scratch_link = self.f.artifacts / "linked-scratch"
        scratch_link.symlink_to(self.f.workspace, target_is_directory=True)
        action = copy.deepcopy(self.action)
        action["verifier_commands"][0]["scratch_root"] = str(scratch_link / "run")
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.compile_context(self.f.workspace, action, artifact_root=self.f.artifacts)

    def test_typed_verifier_rejects_shebang_executable(self):
        command, entry = self.verifier_command()
        executable = self.f.root / "run-verifier.py"
        executable.write_text(f"#!{Path(sys.executable).resolve()}\nprint('verified')\n")
        executable.chmod(0o700)
        command["argv"] = [str(executable), str(entry)]
        command["executable_sha256"] = caller._file_sha256(executable)
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)

    def test_typed_verifier_entry_must_be_in_admitted_read_scopes(self):
        self.verifier_command()
        for entry in (self.f.root / "outside-verifier.py", self.f.workspace / "undeclared-verifier.py"):
            entry.write_text("print('undeclared')\n")
            action = copy.deepcopy(self.action)
            command = action["verifier_commands"][0]
            command["argv"][1] = str(entry)
            command["pinned_files"] = [{"path": str(entry), "sha256": caller._file_sha256(entry)}]
            with self.subTest(entry=entry), self.assertRaisesRegex(
                    caller.EmbeddedNokiyError, "outside admitted read scopes"):
                local.compile_context(self.f.workspace, action, artifact_root=self.f.artifacts)

    def test_outside_verifier_pin_is_rejected_before_file_read(self):
        command, _ = self.verifier_command()
        outside = self.f.root / "outside-unread.py"
        outside.write_text("not admitted\n")
        command["argv"][1] = str(outside)
        command["pinned_files"] = [{"path": str(outside), "sha256": "0" * 64}]
        original = local._verifier_file

        def reject_outside_read(raw, digest, **kwargs):
            self.assertNotEqual(raw, str(outside), "outside bytes must not be read")
            return original(raw, digest, **kwargs)

        with patch.object(local, "_verifier_file", side_effect=reject_outside_read), self.assertRaisesRegex(
                caller.EmbeddedNokiyError, "outside admitted read scopes"):
            local.compile_context(self.f.workspace, self.action, artifact_root=self.f.artifacts)

    def test_typed_verifier_rejects_outside_scratch_network_and_duplicates(self):
        command, _ = self.verifier_command()
        invalid = []
        for scratch in (self.f.root / "outside", self.f.workspace / "scratch", self.f.artifacts):
            changed = copy.deepcopy(self.action)
            changed["verifier_commands"][0]["scratch_root"] = str(scratch)
            invalid.append(changed)
        changed = copy.deepcopy(self.action)
        changed["verifier_commands"][0]["network"] = True
        invalid.append(changed)
        changed = copy.deepcopy(self.action)
        changed["verifier_commands"][0]["pinned_files"].append(copy.deepcopy(command["pinned_files"][0]))
        invalid.append(changed)
        changed = copy.deepcopy(self.action)
        changed["verifier_commands"].append(copy.deepcopy(command))
        invalid.append(changed)
        for action in invalid:
            with self.subTest(action=action["verifier_commands"]), self.assertRaises(caller.EmbeddedNokiyError):
                local.compile_context(self.f.workspace, action, artifact_root=self.f.artifacts)

    def test_cli_without_surface(self):
        parsed = caller.build_parser().parse_args(["prepare", "--request", "draft.json", "--action", "action.json", "--output-dir", "/tmp/new"])
        self.assertIsNone(parsed.surface_id)

    def test_explicit_missing_surface_is_not_downgraded(self):
        with self.assertRaises(caller.EmbeddedNokiyError): self.prepare("invented")
        self.assertFalse(self.output.exists())

    def test_dcf_missing_interpreter_does_not_downgrade(self):
        entry = self.f.workspace / "scripts/ops/dcf.py"
        entry.parent.mkdir(parents=True); entry.write_text("# DCF")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"): self.prepare("s")

    def test_dcf_broken_entry_does_not_downgrade(self):
        entry = self.f.workspace / "scripts/ops/dcf.py"
        entry.parent.mkdir(parents=True); entry.symlink_to("/nonexistent-dcf")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"): self.prepare()

    def test_dcf_ancestor_blocks_subdirectory_downgrade(self):
        entry = self.f.root / "scripts/ops/dcf.py"
        entry.parent.mkdir(parents=True); entry.write_text("# ancestor DCF")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"): self.prepare()

    def test_input_change_rejected_at_execution_boundary(self):
        self.prepare()
        (self.f.workspace / "input.txt").write_text("changed")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_STALE"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_new_dcf_integration_rejected_at_execution_boundary(self):
        self.prepare()
        entry = self.f.workspace / "scripts/ops/dcf.py"
        entry.parent.mkdir(parents=True); entry.write_text("# installed later")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_INVALID"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_local_scope_escape_and_globs_rejected(self):
        for name in ("../outside.txt", "/tmp/outside.txt", "**", ".git/config", "input.txt/../other"):
            with self.subTest(name=name), self.assertRaises(caller.EmbeddedNokiyError):
                local._file(self.f.workspace, name)

    def test_symlink_rejected_even_inside_root(self):
        (self.f.workspace / "link.txt").symlink_to("input.txt")
        with self.assertRaises(caller.EmbeddedNokiyError): local._file(self.f.workspace, "link.txt")

    def test_undeclared_command_read_rejected(self):
        self.action["command_templates"][0]["argv"] = ["cat", "outside.txt"]
        with self.assertRaises(caller.EmbeddedNokiyError): self.prepare()

    def test_shell_command_cannot_claim_read_only(self):
        self.action["command_templates"][0]["argv"] = ["sh", "-c", "touch outside"]
        with self.assertRaises(caller.EmbeddedNokiyError): self.prepare()

    def test_command_operand_binding_required(self):
        self.action["command_templates"][0]["targets"] = []
        with self.assertRaises(caller.EmbeddedNokiyError): self.prepare()

    def test_exact_read_without_command_rejected_before_publication(self):
        self.action.update(operations=["read", "modify"], write_scopes=["input.txt"],
                           command_templates=[])
        self.draft["authority_effect"] = "workspace"
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "no admitted read command"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_exact_read_with_cat_and_modify_passes(self):
        self.action.update(operations=["read", "command", "modify"], write_scopes=["input.txt"])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, _, contract = caller._verify_context(request)
        self.assertEqual(contract["allowed_operations"], ["command", "modify", "read"])
        self.assertEqual(contract["write_scopes"], ["input.txt"])

    def test_partial_exact_cat_coverage_rejected(self):
        (self.f.workspace / "other.txt").write_text("second input\n")
        self.action["read_scopes"].append("other.txt")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "no admitted read command"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_directory_read_command_covers_exact_nested_read(self):
        source = self.f.workspace / "src"
        source.mkdir()
        (source / "nested.txt").write_text("nested input\n")
        self.action.update(read_scopes=["src/nested.txt"], read_directories=["src"],
                           target_paths=["src/nested.txt"], command_templates=[])
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, _, contract = caller._verify_context(request)
        self.assertEqual(contract["read_scopes"], ["src/**", "src/nested.txt"])
        self.assertIn("cat", contract["read_commands"])

    def test_batched_exact_reads_keep_each_operand_bound(self):
        (self.f.workspace / "guard.py").write_text("ENABLED = True\n")
        self.action["read_scopes"].append("guard.py")
        self.action["target_paths"].append("guard.py")
        template = self.action["command_templates"][0]
        template["argv"].append("guard.py")
        template["targets"].append({"operation": "read", "path": "guard.py", "argv_index": 2})
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, _, contract = caller._verify_context(request)
        self.assertEqual(contract["command_templates"], [template])

    def test_batched_read_rejects_wrong_operand_index(self):
        (self.f.workspace / "guard.py").write_text("ENABLED = True\n")
        self.action["read_scopes"].append("guard.py")
        self.action["target_paths"].append("guard.py")
        template = self.action["command_templates"][0]
        template["argv"].append("guard.py")
        template["targets"].append({"operation": "read", "path": "guard.py", "argv_index": 1})
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.prepare()

    def test_workspace_write_needs_explicit_authority(self):
        self.action.update(operations=["read", "command", "create"],
                           write_scopes=["answer.txt"], target_paths=["input.txt", "answer.txt"])
        self.draft["authority_effect"] = "none"
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "AUTHORITY_MISMATCH"): self.prepare()
        self.assertFalse((self.output / "request.json").exists())

    def test_workspace_write_and_absent_target_bound(self):
        self.action.update(operations=["read", "command", "create"],
                           write_scopes=["answer.txt"], target_paths=["input.txt", "answer.txt"])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        (self.f.workspace / "answer.txt").write_text("concurrent owner")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_STALE"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_expired_local_context_cannot_use_dcf_action_freshness(self):
        capsule, contract = local.compile_context(self.f.workspace, self.action)
        gen = capsule["dcf_generation"]
        gen["generated_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        gen["action_freshness"] = {}
        contract["content_sha256"] = caller._canonical_sha256({k:v for k,v in contract.items() if k != "content_sha256"})
        capsule["semantic_sha256"] = caller._canonical_sha256({k:v for k,v in capsule.items() if k != "semantic_sha256"})
        self.f.context.write_text(json.dumps(capsule)); self.f.jspace.write_text(json.dumps(contract))
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "CONTEXT_STALE"):
            caller._verify_context(caller.load_request(self.f._write_request()))


if __name__ == "__main__":
    unittest.main()
