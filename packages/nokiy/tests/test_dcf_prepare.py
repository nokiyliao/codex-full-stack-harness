# SPDX-License-Identifier: MIT
"""Single-process preparation uses project APIs, never a second DCF authority."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_collaboration_harness import dcf_prepare as bridge
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import embedded_nokiy as caller


class DcfPrepareTests(unittest.TestCase):
    def setUp(self):
        self.action = {"navigation_targets": ["symbol:package.item"], "context_summary": "Inspect it.",
                       "operations": ["read"], "read_scopes": ["package.py"], "write_scopes": []}
        self.snapshot = SimpleNamespace(generation_id="generation-1")
        self.runtime = Mock()
        self.runtime.store.load_current.return_value = (self.snapshot, None, Path("/generation-1"))
        self.runtime.store.graph_paths.return_value = (Path("/overlay.sqlite"), Path("/base.sqlite"))
        self.capabilities = {"source-navigation": {"freshness_status": "current",
                                                  "projection_status": "pass", "domain_verdict": "pass"}}
        self.runtime.capability_views.return_value = (None, self.capabilities, None)
        graph = ModuleType("scripts.ops.dcf.graph")
        graph.resolve_entities = Mock(return_value=[{"entity_id": "symbol:package.item",
            "entity_type": "symbol", "payload": {"path": "package.py", "line": 7}}])
        jspace = ModuleType("scripts.ops.dcf.jspace")
        jspace.compile_jspace_contract = Mock(return_value={"dcf_generation": {"generation_id": "generation-1"}})
        jspace.verify_contract_freshness = Mock()
        jspace.compile_task_context_capsule = Mock(return_value={"canonical": True})
        self.graph, self.jspace = graph, jspace
        self.modules = patch.dict(sys.modules, {graph.__name__: graph, jspace.__name__: jspace})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.connect = patch.object(bridge.sqlite3, "connect", side_effect=lambda *a, **k: Mock()).start()
        self.addCleanup(patch.stopall)

    def test_one_generation_readonly_indexes_and_canonical_authority(self):
        original = copy.deepcopy(self.action)
        value = bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.assertEqual(self.action, original)
        self.assertEqual(value["action"]["read_scopes"], original["read_scopes"])
        self.assertEqual(value["action"]["write_scopes"], [])
        self.assertNotIn("navigation_targets", value["action"])
        self.assertIn("package.py:7", value["action"]["context_summary"])
        self.assertEqual(self.connect.call_count, 2)
        for call in self.connect.call_args_list:
            self.assertTrue(call.args[0].endswith("?mode=ro"))
        self.runtime.store.load_current.assert_called_once_with(verify_file_hashes=True)
        self.graph.resolve_entities.assert_called_once()
        self.jspace.compile_jspace_contract.assert_called_once_with(
            self.runtime, surface_id="fixture", action=value["action"])
        self.jspace.verify_contract_freshness.assert_called_once()

    def test_multiple_targets_share_connections_and_generation(self):
        self.action["navigation_targets"].append("symbol:package.other")
        self.graph.resolve_entities.side_effect = lambda con, target, **kw: [{
            "entity_id": target, "entity_type": "symbol", "payload": {"path": "package.py", "line": 8}}]
        value = bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.assertEqual(len(value["navigation_locators"]), 2)
        self.assertEqual(self.connect.call_count, 2)
        self.runtime.capability_views.assert_called_once()

    def test_stale_or_failed_navigation_never_compiles(self):
        for key in self.capabilities["source-navigation"]:
            original = self.capabilities["source-navigation"][key]
            self.capabilities["source-navigation"][key] = "stale"
            with self.assertRaises(ValueError):
                bridge.compile_bundle(self.runtime, "fixture", self.action)
            self.capabilities["source-navigation"][key] = original
        self.jspace.compile_jspace_contract.assert_not_called()
        self.connect.assert_not_called()

    def test_generation_drift_and_freshness_failure_never_return_capsule(self):
        self.jspace.compile_jspace_contract.return_value["dcf_generation"]["generation_id"] = "other"
        with self.assertRaises(ValueError):
            bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.jspace.compile_task_context_capsule.assert_not_called()
        self.jspace.compile_jspace_contract.return_value["dcf_generation"]["generation_id"] = "generation-1"
        self.jspace.verify_contract_freshness.side_effect = ValueError("changed required domain")
        with self.assertRaises(ValueError):
            bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.jspace.compile_task_context_capsule.assert_not_called()

    def test_fuzzy_ambiguous_non_symbol_and_unsafe_locations_rejected(self):
        row = self.graph.resolve_entities.return_value[0]
        for rows in ([], [row, row], [dict(row, entity_id="symbol:package.other")],
                     [dict(row, entity_type="path")],
                     [dict(row, payload={"path": "../outside", "line": 7})],
                     [dict(row, payload={"path": "package.py", "line": True})]):
            self.graph.resolve_entities.return_value = rows
            with self.assertRaises(ValueError):
                bridge.compile_bundle(self.runtime, "fixture", self.action)
        self.jspace.compile_jspace_contract.assert_not_called()

    def test_plain_action_keeps_exact_grants_and_uses_public_apis_once(self):
        action = {"context_summary": "  Inspect exact input.  ", "operations": ["read", "command"],
                  "read_scopes": ["package.py"], "write_scopes": [],
                  "allowed_operations": ["read"], "denied_operations": ["modify"],
                  "command_templates": ["python -B verifier.py"], "include_source_excerpt": False,
                  "mission": {"mission_id": "fixture", "mode": "research"}}
        original = copy.deepcopy(action)
        calls = Mock()
        for name in ("compile_jspace_contract", "verify_contract_freshness", "compile_task_context_capsule"):
            calls.attach_mock(getattr(self.jspace, name), name)
        value = bridge.compile_plain_bundle(self.runtime, "fixture", action)
        self.assertEqual(action, original)
        self.assertIs(value["action"], action)
        self.assertEqual(value["navigation_locators"], [])
        self.assertEqual(value["status"], "compiled_not_admitted")
        self.assertEqual(value["authority_effect"], "none")
        self.assertIs(value["no_apply"], True)
        contract = self.jspace.compile_jspace_contract.return_value
        self.assertIs(value["contract"], contract)
        self.assertIs(value["task_context_capsule"], self.jspace.compile_task_context_capsule.return_value)
        self.assertNotIn("source_read", contract)
        self.assertEqual([call[0] for call in calls.mock_calls], [
            "compile_jspace_contract", "verify_contract_freshness", "compile_task_context_capsule"])
        self.jspace.compile_jspace_contract.assert_called_once_with(self.runtime, surface_id="fixture", action=action)
        self.jspace.verify_contract_freshness.assert_called_once_with(self.runtime, contract)
        self.jspace.compile_task_context_capsule.assert_called_once_with(contract, action=action)
        self.runtime.store.load_current.assert_not_called()
        self.runtime.capability_views.assert_not_called()
        self.runtime.store.graph_paths.assert_not_called()
        self.graph.resolve_entities.assert_not_called()
        self.connect.assert_not_called()

    def test_plain_compile_does_not_import_optional_graph_or_index_modules(self):
        action = dict(self.action)
        action.pop("navigation_targets")
        with patch.dict(sys.modules, {"scripts.ops.dcf.graph": None}):
            bridge.compile_plain_bundle(self.runtime, "fixture", action)
        self.runtime.store.graph_paths.assert_not_called()
        self.connect.assert_not_called()

    def test_plain_compiler_refusal_and_freshness_failure_stop_before_capsule(self):
        action = dict(self.action)
        action.pop("navigation_targets")
        self.jspace.compile_jspace_contract.side_effect = ValueError("canonical refusal")
        with self.assertRaisesRegex(ValueError, "canonical refusal"):
            bridge.compile_plain_bundle(self.runtime, "fixture", action)
        self.jspace.compile_jspace_contract.assert_called_once()
        self.jspace.verify_contract_freshness.assert_not_called()
        self.jspace.compile_task_context_capsule.assert_not_called()
        self.jspace.compile_jspace_contract.reset_mock()
        self.jspace.compile_jspace_contract.side_effect = None
        self.jspace.verify_contract_freshness.side_effect = ValueError("generation or required domain changed")
        with self.assertRaisesRegex(ValueError, "generation or required domain changed"):
            bridge.compile_plain_bundle(self.runtime, "fixture", action)
        self.jspace.compile_jspace_contract.assert_called_once()
        self.jspace.verify_contract_freshness.assert_called_once()
        self.jspace.compile_task_context_capsule.assert_not_called()

    def test_main_selects_plain_only_when_navigation_targets_key_is_absent(self):
        self.jspace.JSpaceCompileError = type("JSpaceCompileError", (ValueError,), {})
        runtime = ModuleType("scripts.ops.dcf.runtime")
        runtime.DcfRuntime = Mock(return_value=self.runtime)
        for targets in (None, [], ["symbol:package.item"]):
            action = copy.deepcopy(self.action)
            if targets is None:
                action.pop("navigation_targets")
            else:
                action["navigation_targets"] = targets
            output = io.BytesIO()
            source = io.BytesIO(json.dumps({"surface_id": "fixture", "action": action}).encode())
            with (patch.dict(sys.modules, {runtime.__name__: runtime}),
                  patch.object(sys, "stdin", SimpleNamespace(buffer=source)),
                  patch.object(sys, "stdout", SimpleNamespace(buffer=output)),
                  patch.object(sys, "path", list(sys.path)),
                  patch.object(bridge, "compile_bundle", return_value={}) as navigated,
                  patch.object(bridge, "compile_plain_bundle", return_value={}) as plain):
                self.assertEqual(bridge.main(), 0)
            selected, unselected = (plain, navigated) if targets is None else (navigated, plain)
            selected.assert_called_once_with(self.runtime, "fixture", action)
            unselected.assert_not_called()

    def test_plain_main_retains_canonical_error_diagnostic_without_raw_message(self):
        diagnostic = {"capability_id": "surface-map", "failed_field": "freshness_status",
                      "observed": "stale", "expected": "current", "generation_id": "generation-1"}

        class JSpaceCompileError(ValueError):
            def __init__(self):
                super().__init__("NOKIY_PLAIN_ERROR_SECRET_SENTINEL")
                self.error_code = "JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT"
                self.blocked = True
                self.capability_diagnostic = diagnostic

        self.jspace.JSpaceCompileError = JSpaceCompileError
        runtime = ModuleType("scripts.ops.dcf.runtime")
        runtime.DcfRuntime = Mock(return_value=self.runtime)
        action = dict(self.action)
        action.pop("navigation_targets")
        for stage in ("compile_jspace_contract", "verify_contract_freshness"):
            output = io.BytesIO()
            source = io.BytesIO(json.dumps({"surface_id": "fixture", "action": action}).encode())
            self.jspace.compile_jspace_contract.reset_mock(side_effect=True)
            self.jspace.verify_contract_freshness.reset_mock(side_effect=True)
            getattr(self.jspace, stage).side_effect = JSpaceCompileError()
            with (patch.dict(sys.modules, {runtime.__name__: runtime, "scripts.ops.dcf.graph": None}),
                  patch.object(sys, "stdin", SimpleNamespace(buffer=source)),
                  patch.object(sys, "stdout", SimpleNamespace(buffer=output)),
                  patch.object(sys, "path", list(sys.path))):
                self.assertEqual(bridge.main(), 2)
            self.assertEqual(json.loads(output.getvalue()), {
                "schema_version": "jspace_error_v1", "status": "blocked",
                "error_code": "JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT", "capability_diagnostic": diagnostic,
                "authority_effect": "none", "no_apply": True})
            self.assertNotIn(b"SECRET_SENTINEL", output.getvalue())
            self.jspace.compile_jspace_contract.assert_called_once_with(self.runtime, surface_id="fixture", action=action)
            self.jspace.compile_task_context_capsule.assert_not_called()
        self.connect.assert_not_called()

    def test_transport_errors_do_not_retry_or_downgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            compiler = Path(temp) / "dcf.py"
            compiler.write_text("# fixture")
            for result in (subprocess.TimeoutExpired("fixture", 60), OSError("fixture"),
                           subprocess.CompletedProcess([], 2, "{}"),
                           subprocess.CompletedProcess([], 0, "{}"),
                           subprocess.CompletedProcess([], 0, "x" * (caller.MAX_REQUEST_BYTES + 1))):
                kwargs = ({"side_effect": result} if isinstance(result, Exception) else {"return_value": result})
                with (patch.object(stack, "_dcf_interpreter_identity", return_value=("fixture",)),
                      patch.object(stack.subprocess, "run", **kwargs) as run):
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                        stack._compile_navigated_dcf(Path(temp), compiler, Path(sys.executable), "fixture", self.action)
                run.assert_called_once()

    def test_navigated_compiler_failure_retains_typed_cause_without_raw_output(self):
        secret = "NOKIY_NAVIGATED_FAILURE_SECRET_SENTINEL"
        stdout = json.dumps({"schema_version": "jspace_error_v1", "status": "blocked",
                             "error_code": "JSPACE_SOURCE_READ_FILE_INVALID", "error": secret,
                             "authority_effect": "none", "no_apply": True})
        with tempfile.TemporaryDirectory() as temp:
            compiler = Path(temp) / "dcf.py"
            compiler.write_text("# fixture")
            with (patch.object(stack, "_dcf_interpreter_identity", return_value=("fixture",)),
                  patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, stdout, secret)) as run):
                with self.assertRaises(caller.EmbeddedNokiyError) as error:
                    stack._compile_navigated_dcf(Path(temp), compiler, Path(sys.executable), "fixture", self.action)
        failure = caller._failure(error.exception)
        self.assertEqual(failure["first_typed_blocker"], "NOKIY_FULL_STACK_NAVIGATION_UNVERIFIED")
        self.assertEqual(failure["diagnostics"]["child_error_code"], "JSPACE_SOURCE_READ_FILE_INVALID")
        self.assertEqual(failure["diagnostics"]["stage"], "dcf_navigated_prepare")
        self.assertEqual(failure["diagnostics"]["stdout_sha256"], hashlib.sha256(stdout.encode()).hexdigest())
        self.assertEqual(failure["diagnostics"]["stderr_sha256"], hashlib.sha256(secret.encode()).hexdigest())
        self.assertNotIn(secret, json.dumps(failure))
        run.assert_called_once()

    def test_bridge_main_emits_only_bounded_no_effect_typed_failure(self):
        class JSpaceCompileError(ValueError):
            def __init__(self, code, *, blocked):
                super().__init__("NOKIY_BRIDGE_ERROR_SECRET_SENTINEL")
                self.error_code, self.blocked = code, blocked

        self.jspace.JSpaceCompileError = JSpaceCompileError
        runtime = ModuleType("scripts.ops.dcf.runtime")
        runtime.DcfRuntime = Mock(return_value=self.runtime)
        for code, blocked in (("JSPACE_SOURCE_READ_FILE_INVALID", True),
                              ("JSPACE_READ_SCOPE_MISSING", False),
                              ("JSPACE_COMMAND_EFFECT_UNVERIFIED", True),
                              ("TASK_CONTEXT_FIELDS_INVALID", False),
                              ("JSPACE_NOKIY_SECRET_SENTINEL raw message", True)):
            output = io.BytesIO()
            source = io.BytesIO(json.dumps({"surface_id": "fixture", "action": self.action}).encode())
            with (patch.dict(sys.modules, {runtime.__name__: runtime}),
                  patch.object(sys, "stdin", SimpleNamespace(buffer=source)),
                  patch.object(sys, "stdout", SimpleNamespace(buffer=output)),
                  patch.object(sys, "path", list(sys.path)),
                  patch.object(bridge, "compile_bundle", side_effect=JSpaceCompileError(code, blocked=blocked)) as compile_bundle):
                self.assertEqual(bridge.main(), 2)
            payload = json.loads(output.getvalue())
            expected = {"schema_version": "jspace_error_v1", "status": "blocked" if blocked else "invalid",
                        "authority_effect": "none", "no_apply": True}
            if caller._PREPARATION_CHILD_CODE.fullmatch(code):
                expected["error_code"] = code
            self.assertEqual(payload, expected)
            self.assertNotIn(b"SECRET_SENTINEL", output.getvalue())
            compile_bundle.assert_called_once()
        self.assertEqual(bridge._PREPARATION_CHILD_CODE.pattern, caller._PREPARATION_CHILD_CODE.pattern)

    def test_bridge_main_types_a_workspace_dcf_without_jspace(self):
        output = io.BytesIO()
        source = io.BytesIO(json.dumps({"surface_id": "fixture", "action": self.action}).encode())
        with (patch.dict(sys.modules, {"scripts.ops.dcf.jspace": None}),
              patch.object(sys, "stdin", SimpleNamespace(buffer=source)),
              patch.object(sys, "stdout", SimpleNamespace(buffer=output)),
              patch.object(sys, "path", list(sys.path))):
            self.assertEqual(bridge.main(), 2)
        self.assertEqual(json.loads(output.getvalue()), {
            "schema_version": "jspace_error_v1", "status": "blocked", "error_code": "JSPACE_MODULE_UNAVAILABLE",
            "authority_effect": "none", "no_apply": True})
        self.assertEqual(source.tell(), 0)
        completed = subprocess.CompletedProcess([], 2, output.getvalue().decode(), "")
        self.assertEqual(stack._dcf_failure_diagnostics(completed, "dcf_compile")["child_error_code"],
                         "JSPACE_MODULE_UNAVAILABLE")

    def test_navigated_failure_preserves_canonical_namespaces_without_running_worker(self):
        for code in ("JSPACE_COMMAND_EFFECT_UNVERIFIED", "JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT",
                     "TASK_CONTEXT_FIELDS_INVALID", "JSPACE_FUTURE_DIAGNOSTIC"):
            stdout = json.dumps({"schema_version": "jspace_error_v1", "status": "blocked",
                                 "error_code": code, "authority_effect": "none", "no_apply": True})
            completed = subprocess.CompletedProcess([], 2, stdout, "secret detail")
            with self.subTest(code=code):
                result = stack._dcf_failure_diagnostics(completed, "dcf_navigated_prepare")
                self.assertEqual(result["child_error_code"], code)
                self.assertNotIn("secret detail", json.dumps(result))


class RuntimeVerifierGrammarTests(unittest.TestCase):
    """The bridge binds the installed runtime's verifier grammar into any DCF generation."""

    class Refusal(ValueError):
        def __init__(self, code, message, *, blocked):
            super().__init__(message)
            self.error_code, self.blocked = code, blocked

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve(strict=True)
        self.workspace, self.artifacts, self.external = root / "workspace", root / "artifacts", root / "external"
        for directory in (self.workspace / "tests", self.artifacts / "scratch", self.external):
            directory.mkdir(parents=True)
        self.python = Path(sys.executable).resolve(strict=True)
        self.entry = self.workspace / "tests" / "check.py"
        self.entry.write_text("#!/usr/bin/env python3\nprint('ok')\n")
        self.validate = bridge.runtime_verifier_grants(self.Refusal)

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def payload(self, executable, entries, *, argv=None, **extra):
        argv = argv or [str(executable), "-B", *(str(path) for path, _ in entries)]
        command = {"argv": argv, "executable_sha256": self.digest(executable),
                   "pinned_files": [dict({"path": str(path), "sha256": self.digest(path)}, **scope)
                                    for path, scope in entries],
                   "timeout_seconds": 60, "scratch_root": str(self.artifacts / "scratch"), "network": False,
                   **extra}
        return {"repo_root": str(self.workspace), "verifier_artifact_root": str(self.artifacts),
                "allowed_operations": ["command", "read"], "denied_operations": ["network"],
                "read_scopes": ["tests/check.py"], "verifier_commands": [command]}

    def test_admits_current_runtime_fields_older_dcf_generations_refuse(self):
        external = self.external / "acceptance.py"
        external.write_text("print('external')\n")
        for payload in (
                self.payload(self.python, [(self.entry, {})], python_import_roots=[str(self.workspace)],
                             cwd=str(self.workspace)),
                self.payload(self.python, [], argv=[str(self.python), "-B", "-m", "pytest", "-q"]),
                self.payload(self.python, [(external, {"scope": "verifier_only"})]),
                self.payload(self.python, [(self.entry, {})],
                             argv=[str(self.python), "-B", str(self.entry), str(self.entry)])):
            with self.subTest(argv=payload["verifier_commands"][0]["argv"][2:]):
                self.assertIsNone(self.validate(payload))

    def test_interpreter_entry_header_is_data_but_an_exec_operand_header_is_not(self):
        runner = self.external / "runner"
        runner.write_bytes(self.python.read_bytes())
        runner.chmod(0o700)
        for header in ("#!/usr/bin/env python3", "#!/usr/bin/env -S node", "#!/bin/sh", "#!/usr/bin/env bash"):
            self.entry.write_text(header + "\nprint('ok')\n")
            with self.subTest(header=header):
                self.assertIsNone(self.validate(self.payload(self.python, [(self.entry, {})])))
                with self.assertRaisesRegex(self.Refusal, "shell or env") as refused:
                    self.validate(self.payload(runner, [(self.entry, {})], argv=[str(runner), str(self.entry)]))
                self.assertEqual(refused.exception.error_code, "JSPACE_VERIFIER_GRANT_INVALID")
        self.entry.write_text("print('no header')\n")
        self.assertIsNone(self.validate(self.payload(runner, [(self.entry, {})], argv=[str(runner), str(self.entry)])))

    def test_keeps_scope_pin_scratch_and_network_boundaries(self):
        scratch_pin = self.artifacts / "scratch" / "pin.py"
        scratch_pin.write_text("print('scratch')\n")
        inside = self.payload(self.python, [(self.entry, {"scope": "verifier_only"})])
        unread = self.payload(self.python, [(self.entry, {})])
        unread["read_scopes"] = []
        stale = self.payload(self.python, [(self.entry, {})])
        stale["verifier_commands"][0]["pinned_files"][0]["sha256"] = "0" * 64
        networked = self.payload(self.python, [(self.entry, {})], network=True)
        shell = self.payload(Path("/bin/sh"), [(self.entry, {})])
        unknown = self.payload(self.python, [(self.entry, {})], environment={})
        for name, payload, reason in (
                ("verifier_only inside workspace", inside, "outside workspace"),
                ("verifier_only in scratch", self.payload(self.python, [(scratch_pin, {"scope": "verifier_only"})]),
                 "scratch root"),
                ("entry outside read scope", unread, "read scope"),
                ("changed pin", stale, "bytes changed"),
                ("network", networked, "network policy"),
                ("shell executable", shell, "shells"),
                ("unknown field", unknown, "fields differ")):
            with self.subTest(name):
                with self.assertRaisesRegex(self.Refusal, reason):
                    self.validate(payload)

    def test_verifier_only_node_program_runs_directly_outside_credentials(self):
        node = self.external / "node"
        node.write_bytes(self.python.read_bytes())
        node.chmod(0o700)
        program = self.external / "npm-cli.js"
        program.write_text("#!/usr/bin/env node\nconsole.log('ok')\n")
        pin = [(program, {"scope": "verifier_only"})]
        self.assertIsNone(self.validate(self.payload(node, pin, argv=[str(node), str(program), "test"])))
        (self.external / ".ssh").mkdir()
        hidden = self.external / ".ssh" / "cli.js"
        hidden.write_bytes(program.read_bytes())
        text = self.external / "cli.txt"
        text.write_bytes(program.read_bytes())
        for argv, entries in (([str(node), "--require", str(program)], pin),
                              ([str(node), str(hidden)], [(hidden, {"scope": "verifier_only"})]),
                              ([str(node), str(text)], [(text, {"scope": "verifier_only"})])):
            with self.subTest(argv=argv[1:]):
                with self.assertRaisesRegex(self.Refusal, "direct JavaScript program"):
                    self.validate(self.payload(node, entries, argv=argv))

    def test_module_options_and_literal_operands_are_neither_inline_code_nor_shell(self):
        # 2026-10-26 (V430): pytest -c pytest.ini and unittest -p test_*.py were refused as inline
        # code and shell syntax, and a recursive read scope did not cover its pinned file.
        def module(*rest):
            return self.payload(self.python, [], argv=[str(self.python), "-B", "-m", *rest])

        for payload in (module("pytest", "-q", "-c", "pytest.ini", "--rootdir=."),
                        module("unittest", "discover", "-s", "tests", "-p", "test_*.py"),
                        module("pytest", "-q", "tests/test_x.py::case[a-b]", "-k", "(a or b) and not {c}")):
            with self.subTest(argv=payload["verifier_commands"][0]["argv"][3:]):
                self.assertIsNone(self.validate(payload))
        covered = self.payload(self.python, [(self.entry, {})])
        covered["read_scopes"] = ["tests/**"]
        self.assertIsNone(self.validate(covered))
        for argv in ([str(self.python), "-c", "pass"], [str(self.python), "-Bc", "pass"],
                     [str(self.python), "-W", "ignore", "-c", "pass"],
                     [str(self.python), "-Wignore", "-c", "pass"]):
            with self.subTest(argv=argv[1:]):
                with self.assertRaisesRegex(self.Refusal, "inline interpreter"):
                    self.validate(self.payload(self.python, [], argv=argv))
        for operand in ("tests;rm", "$HOME", "a|b", "'quoted'"):
            with self.subTest(operand=operand):
                with self.assertRaisesRegex(self.Refusal, "shell-free"):
                    self.validate(module("pytest", "-q", operand))

    def test_work_product_verifier_needs_no_pin_but_its_program_does(self):
        payload = self.payload(self.python, [], argv=[str(self.python), "-B", str(self.entry)])
        with self.assertRaisesRegex(self.Refusal, "immutable entry files"):
            self.validate(payload)
        payload["write_scopes"] = ["tests/check.py"]
        self.assertIsNone(self.validate(payload))
        program = self.external / "runner.py"
        program.write_text("print('runner')\n")
        payload["verifier_commands"][0]["argv"].insert(2, str(program))
        with self.assertRaisesRegex(self.Refusal, "immutable entry files"):
            self.validate(payload)

    def test_main_binds_the_grammar_before_any_project_compilation(self):
        jspace = ModuleType("scripts.ops.dcf.jspace")
        jspace.JSpaceCompileError = self.Refusal
        jspace._validate_verifier_grants = Mock(side_effect=AssertionError("project grammar used"))
        runtime = ModuleType("scripts.ops.dcf.runtime")
        runtime.DcfRuntime = Mock()
        seen = []

        def compile_plain(*_):
            seen.append(jspace._validate_verifier_grants)
            return {}

        source = io.BytesIO(json.dumps({"surface_id": "fixture", "action": {"operations": ["read"]}}).encode())
        search_path = list(sys.path)
        with (patch.dict(sys.modules, {jspace.__name__: jspace, runtime.__name__: runtime}),
              patch.dict("os.environ", {"NOKIY_DCF_CODE_ROOT": "/primary-dcf-code"}),
              patch.object(sys, "stdin", SimpleNamespace(buffer=source)),
              patch.object(sys, "stdout", SimpleNamespace(buffer=io.BytesIO())),
              patch.object(sys, "path", search_path),
              patch.object(bridge, "compile_plain_bundle", side_effect=compile_plain)):
            self.assertEqual(bridge.main(), 0)
        # The project's data stays the working directory; its DCF code may be the primary's.
        self.assertEqual(search_path[0], "/primary-dcf-code")
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0], jspace._validate_verifier_grants)
        self.assertEqual(seen[0].__qualname__, "runtime_verifier_grants.<locals>.validate")
        self.assertIn("install_verifier_grammar()", stack._with_verifier_grammar("pass"))


class OperatorWriteAdmissionTests(unittest.TestCase):
    """Exact declared writes outside the context surface carry the operator's Nokiy authority."""

    class Refusal(ValueError):
        def __init__(self, code, message, *, blocked=True):
            super().__init__(message)
            self.error_code, self.blocked = code, blocked

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        (self.root / "engine").mkdir()
        (self.root / "ui/dashboard").mkdir(parents=True)
        (self.root / "ui/dashboard/app.py").write_text("app = 1\n")
        (self.root / "elsewhere").mkdir()
        (self.root / "linked").symlink_to(self.root / "elsewhere", target_is_directory=True)
        refusal = self.Refusal

        def surface_check(surface, scopes):
            for scope in scopes:
                if scope.startswith("/") or ".." in Path(scope).parts:
                    raise refusal("JSPACE_WRITE_SCOPE_OUTSIDE_SURFACE", "traversal")
                if not scope.startswith("engine/"):
                    raise refusal("JSPACE_WRITE_SCOPE_OUTSIDE_SURFACE", "outside")

        self.jspace = ModuleType("scripts.ops.dcf.jspace")
        self.jspace.JSpaceCompileError = refusal
        self.jspace._validate_write_scopes = surface_check
        bridge.install_operator_write_admission(self.jspace, self.root)

    def test_exact_files_outside_the_surface_are_admitted(self):
        surface = {"surface_id": "runtime"}
        self.assertIsNone(self.jspace._validate_write_scopes(
            surface, ["engine/kernel.py", "ui/dashboard/app.py", "ui/dashboard/new_view.py"]))

    def test_a_read_only_surface_is_context_that_admits_only_exact_operator_writes(self):
        rows = {"surfaces": [{"surface_id": "projection", "read_only": True, "allowed_write_scopes": ["ui/**"]},
                             {"surface_id": "runtime", "allowed_write_scopes": ["engine/**"]}]}
        jspace = ModuleType("scripts.ops.dcf.jspace")
        jspace.JSpaceCompileError = self.Refusal

        def surface_check(surface, scopes):
            for scope in scopes:
                if not any(scope.startswith(allowed[:-2]) for allowed in surface["allowed_write_scopes"]):
                    raise self.Refusal("JSPACE_WRITE_SCOPE_OUTSIDE_SURFACE", "outside")

        jspace._validate_write_scopes = surface_check
        jspace._surface = lambda data, surface_id: next(
            row for row in data["surfaces"] if row["surface_id"] == surface_id)
        bridge.install_operator_write_admission(jspace, self.root)
        context = jspace._surface(rows, "projection")
        self.assertEqual((context["read_only"], context["allowed_write_scopes"]), (False, []))
        self.assertTrue(rows["surfaces"][0]["read_only"], "surface-map data is not mutated")
        self.assertEqual(jspace._surface(rows, "runtime"), rows["surfaces"][1])
        self.assertIsNone(jspace._validate_write_scopes(context, ["ui/dashboard/app.py", "ui/dashboard/new.tsx"]))
        for scope in ("ui/**", "ui/*.py", "linked/file.py", ".git/config"):
            with self.subTest(scope=scope):
                with self.assertRaises(self.Refusal):
                    jspace._validate_write_scopes(context, [scope])

    def test_patterns_traversal_git_and_links_keep_the_surface_refusal(self):
        for scope in ("ui/dashboard/**", "ui/*.py", "../outside.py", "/abs/path.py", ".git/config",
                      "ui/.git/hooks/x", "linked/file.py", "ui/dashboard/", "ui/dashboard"):
            with self.subTest(scope=scope):
                with self.assertRaises(self.Refusal):
                    self.jspace._validate_write_scopes({"surface_id": "runtime"}, [scope])

    def test_other_compiler_refusals_are_never_converted(self):
        def other(surface, scopes):
            raise self.Refusal("JSPACE_TARGET_INVALID", "other")
        jspace = ModuleType("scripts.ops.dcf.jspace")
        jspace.JSpaceCompileError, jspace._validate_write_scopes = self.Refusal, other
        bridge.install_operator_write_admission(jspace, self.root)
        with self.assertRaises(self.Refusal) as refused:
            jspace._validate_write_scopes({"surface_id": "runtime"}, ["ui/dashboard/app.py"])
        self.assertEqual(refused.exception.error_code, "JSPACE_TARGET_INVALID")


if __name__ == "__main__":
    unittest.main()
