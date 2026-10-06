# SPDX-License-Identifier: MIT
"""New-request preparation only; real preflight with a synthetic pinned image."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import test_full_core as fixtures
import test_embedded_nokiy as embedded_fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import local_context


class FullStackTests(unittest.TestCase):
    def setUp(self):
        # DCF mocks must not intercept another module's real runtime probes.
        process_api = patch.object(stack, "subprocess", SimpleNamespace(**vars(subprocess)))
        process_api.start()
        self.addCleanup(process_api.stop)
        self.core = fixtures.FullCoreTests()
        self.core.setUp()
        self.addCleanup(self.core.doCleanups)
        self.f = self.core.f
        self.draft = json.loads(self.f.request_path.read_text())
        for key in ("context_capsule", "jspace_contract", "execution_profile",
                    "model", "reasoning_effort", "service_tier", "model_acceleration"):
            self.draft.pop(key, None)
        self.action = self.f.root / "action.json"
        self.action.write_text(json.dumps({"mission": {"task_id": "test"}, "operations": ["read"]}))
        self.output = self.f.root / "prepared"
        compiler = self.f.workspace / "scripts/ops/dcf.py"
        compiler.parent.mkdir(parents=True)
        compiler.write_text("# synthetic DCF entry\n")
        python = self.f.workspace / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("synthetic interpreter")
        self.compiled = {"task_context_capsule": json.loads(self.f.context.read_text()),
                         "contract": json.loads(self.f.jspace.read_text())}

    def run_prepare(self, surface_id="research_execution_engine"):
        self.f.request_path.write_text(json.dumps(self.draft))
        return stack.prepare_request(self.f.request_path, self.action, surface_id, self.output)

    def compiler(self):
        return patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(self.compiled), ""))

    def compiled_navigation(self, action, navigation):
        from codex_collaboration_harness.dcf_prepare import bind_locators
        amended = {key: value for key, value in action.items()
                   if key not in {"surface_targets", "include_source_excerpt"}}
        navigation = navigation if isinstance(navigation, list) else [navigation]
        locators = []
        for item in navigation:
            row = item["result"]["resolved"][0]
            locators.append({"target": item["target"], "path": row["payload"]["path"],
                             "line": row["payload"]["line"], "generation_id": item["generation_id"]})
        amended = bind_locators(amended, locators, navigation[0]["generation_id"])
        compiled = json.loads(json.dumps(self.compiled))
        capsule = compiled["task_context_capsule"]
        capsule["context_summary"] = amended["context_summary"]
        capsule["semantic_sha256"] = caller._canonical_sha256(
            {key: value for key, value in capsule.items() if key != "semantic_sha256"})
        return dict(compiled, action=amended, navigation_locators=locators)

    def assert_public_action_identity(self, result, action):
        raw = caller._canonical_bytes(action)
        expected = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        summary = action.get("context_summary")
        if isinstance(summary, str):
            expected["context_summary_sha256"] = hashlib.sha256(summary.encode()).hexdigest()
            expected["context_summary_chars"] = len(summary)
        self.assertEqual(result["action"], expected)

    def test_default_direct_full_stack_real_preflight_no_provider(self):
        with self.compiler() as compiler, patch.object(caller, "execute") as execute:
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(compiler.call_count, 1)  # legacy exact ID pays no lookup
        self.assertEqual(result["status"], "PREPARED")
        self.assertEqual(result["execution_profile"], "direct")
        request = caller.load_request(self.output / "request.json")
        self.assertEqual((request.model, request.reasoning_effort, request.service_tier),
                         ("gpt-6.1-sol", "max", "default"))
        self.assertEqual(request.max_trajectory_bytes, self.draft["max_trajectory_bytes"])
        self.assertEqual(caller.preflight(request)["status"], "READY")
        self.assertIn("--action-stdin", compiler.call_args.args[0])
        self.assertFalse(result["provider_execution_started"])

    def test_terminal_delivery_is_derived_from_action_only_and_not_sent_to_compiler(self):
        for profile in ("direct", "balanced"):
            with self.subTest(profile=profile):
                self.output = self.f.root / ("prepared-terminal-" + profile)
                self.draft["execution_profile"] = profile
                action = {"mission": {"task_id": "test"}, "operations": ["read"],
                          "terminal_delivery": "evidence_only"}
                self.action.write_text(json.dumps(action))
                with self.compiler() as compiler, patch.object(caller, "execute") as execute:
                    result = self.run_prepare()
                execute.assert_not_called()
                self.assertEqual(compiler.call_count, 1)
                request = caller.load_request(self.output / "request.json")
                self.assertEqual(request.terminal_delivery, "evidence_only")
                self.assertEqual(request.to_wire()["terminal_delivery"], "evidence_only")
                self.assertNotIn("terminal_delivery", self.draft)
                compiled_action = json.loads((self.output / "preparation.json").read_text())["action"]
                self.assertEqual(compiled_action, {key: value for key, value in action.items()
                                                   if key != "terminal_delivery"})
                self.assertFalse(result["provider_execution_started"])

    def test_terminal_delivery_explicit_default_is_omitted_from_prepared_wire(self):
        self.action.write_text(json.dumps({"mission": {"task_id": "test"}, "operations": ["read"],
                                          "terminal_delivery": "assistant_reply"}))
        with self.compiler():
            self.run_prepare()
        wire = json.loads((self.output / "request.json").read_text())
        self.assertNotIn("terminal_delivery", wire)
        request = caller.decode_request(wire)
        self.assertEqual(request.request_sha256, caller._canonical_sha256(wire))
        self.assertEqual(request.terminal_delivery, "assistant_reply")

    def test_terminal_delivery_invalid_action_or_draft_does_not_compile_or_publish(self):
        for mode in (None, "unknown", "", 1, True, {}, []):
            self.action.write_text(json.dumps({"mission": {"task_id": "test"}, "operations": ["read"],
                                              "terminal_delivery": mode}))
            with self.subTest(mode=mode), self.compiler() as compiler:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "terminal_delivery"):
                    self.run_prepare()
                compiler.assert_not_called()
                self.assertFalse(self.output.exists())
        self.action.write_text(json.dumps({"mission": {"task_id": "test"}, "operations": ["read"]}))
        self.draft["terminal_delivery"] = "evidence_only"
        with self.compiler() as compiler:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "terminal_delivery belongs in action"):
                self.run_prepare()
            compiler.assert_not_called()
            self.assertFalse(self.output.exists())

    def test_terminal_delivery_action_requires_v3_full_core_profile(self):
        self.action.write_text(json.dumps({"mission": {"task_id": "test"}, "operations": ["read"],
                                          "terminal_delivery": "evidence_only"}))
        for changes in ({"schema_version": caller.LEGACY_REQUEST_SCHEMA_VERSION},
                        {"schema_version": caller.PREVIOUS_REQUEST_SCHEMA_VERSION},
                        {"execution_profile": "native_once"}):
            original = dict(self.draft)
            self.draft.update(changes)
            with self.subTest(changes=changes), self.compiler() as compiler:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "full-core profile required"):
                    self.run_prepare()
                compiler.assert_not_called()
                self.assertFalse(self.output.exists())
            self.draft = original

    def test_prepare_missing_full_core_trajectory_budget_defaults_to_null(self):
        self.draft.pop("max_trajectory_bytes")
        with self.compiler(), patch.object(caller, "execute") as execute:
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["status"], "PREPARED")
        request = caller.load_request(self.output / "request.json")
        self.assertIsNone(request.max_trajectory_bytes)
        self.assertIsNone(request.to_wire(include_identity=False)["max_trajectory_bytes"])

    def test_explicit_tier_prepare_preflight_and_cli_preserve_unknown_delivery(self):
        # Explicit caller tier support is independent of the retired Fast alias.
        profile = {"model": "gpt-6-sol", "reasoning_effort": "max",
                   "execution_profile": "direct", "service_tier": "priority"}
        self.draft.update(profile)
        with self.compiler(), patch.object(caller, "execute") as execute:
            prepared = self.run_prepare()
        execute.assert_not_called()
        request = caller.load_request(self.output / "request.json")
        self.assertEqual({key: getattr(request, key) for key in profile}, profile)
        ready = prepared["preflight"]
        self.assertEqual(ready["status"], "READY")
        self.assertEqual((ready["model"], ready["reasoning_effort"], ready["execution_profile"],
                          ready["requested_service_tier"], ready["observed_service_tier"]),
                         ("gpt-6-sol", "max", "direct", "priority", None))
        runtime = SimpleNamespace(artifacts={"tura_exec": SimpleNamespace(path=self.f.root / "tura")})
        argv = full_core._cli_argv(request, runtime, self.f.root, "test-address")
        for flag, value in (("-m", "codex/gpt-6-sol"), ("-a", "direct"),
                            ("--model-reasoning-effort", "max"), ("--service-tier", "priority")):
            self.assertEqual(argv[argv.index(flag) + 1], value)

        default = caller.decode_request({**request.to_wire(include_identity=False),
                                         "service_tier": "default"})
        self.assertNotEqual((request.request_id, request.request_sha256),
                            (default.request_id, default.request_sha256))
        self.assertEqual((request.context_capsule, request.jspace_contract),
                         (default.context_capsule, default.jspace_contract))
        self.assertEqual(json.loads((self.output / "preparation.json").read_text())["action"],
                         json.loads(self.action.read_text()))
        self.assertEqual(json.loads(self.action.read_text())["operations"], ["read"])

    def test_dcf_prepare_cli_omits_raw_action_but_preserves_durable_inputs(self):
        summary = "DCF_CONTEXT_SENTINEL_" + "x" * 11900
        projection = "DCF_PROJECTION_SENTINEL_" + "y" * 1000
        action = {"mission": {"task_id": "test"}, "operations": ["read"],
                  "context_summary": summary, "task_projection": {"note": projection}}
        self.action.write_text(json.dumps(action))
        self.f.request_path.write_text(json.dumps(self.draft))
        capsule = self.compiled["task_context_capsule"]
        capsule["context_summary"] = summary
        capsule["semantic_sha256"] = caller._canonical_sha256(
            {key: value for key, value in capsule.items() if key != "semantic_sha256"})
        stdout = io.StringIO()
        with self.compiler(), redirect_stdout(stdout):
            exit_code = caller.main(["prepare", "--request", str(self.f.request_path),
                                     "--action", str(self.action), "--surface-id",
                                     "research_execution_engine", "--output-dir", str(self.output)])
        self.assertEqual(exit_code, 0)
        public_json = stdout.getvalue()
        result = json.loads(public_json)
        self.assert_public_action_identity(result, action)
        self.assertNotIn("DCF_CONTEXT_SENTINEL_", public_json)
        self.assertNotIn("DCF_PROJECTION_SENTINEL_", public_json)
        self.assertEqual(json.loads((self.output / "preparation.json").read_text())["action"], action)
        self.assertEqual(json.loads((self.output / "capsule.json").read_text()), capsule)
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), self.compiled["contract"])
        caller.load_request(self.output / "request.json")
        old_public = {**result, "action": action}
        self.public_json_before_bytes = len(json.dumps(old_public, ensure_ascii=True, sort_keys=True).encode()) + 1
        self.public_json_after_bytes = len(public_json.encode())
        self.assertGreater(self.public_json_before_bytes - self.public_json_after_bytes, len(summary))

    def test_local_prepare_omits_raw_action_but_preserves_durable_inputs(self):
        (self.f.workspace / "scripts/ops/dcf.py").unlink()
        (self.f.workspace / "input.txt").write_text("bounded input\n")
        summary = "LOCAL_CONTEXT_SENTINEL_" + "z" * 11900
        action = {"mission": {"mission_id": "local", "task_id": "test", "mode": "DELIVERY",
                              "objective": "Read one input", "current_predicate": "input_read"},
                  "context_summary": summary, "operations": ["read", "command"],
                  "read_scopes": ["input.txt"], "write_scopes": [],
                  "target_paths": ["input.txt"], "command_templates": [{
                      "argv": ["cat", "input.txt"], "effects": ["read"],
                      "targets": [{"operation": "read", "path": "input.txt", "argv_index": 1}]}],
                  "forbidden_effects": []}
        self.action.write_text(json.dumps(action))
        self.f.request_path.write_text(json.dumps(self.draft))
        result = stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assert_public_action_identity(result, action)
        self.assertNotIn("LOCAL_CONTEXT_SENTINEL_", json.dumps(result))
        self.assertEqual(json.loads((self.output / "preparation.json").read_text())["action"], action)
        capsule = json.loads((self.output / "capsule.json").read_text())
        contract = json.loads((self.output / "jspace.json").read_text())
        self.assertEqual(capsule["context_summary"], summary)
        self.assertEqual(contract["dcf_generation"], capsule["dcf_generation"])
        self.assertEqual(result["context_mode"], local_context.MODE)
        caller._verify_context(caller.load_request(self.output / "request.json"))

    def test_explicit_profile_preserved(self):
        self.draft.update(execution_profile="balanced", model="gpt-5.6-sol", reasoning_effort="xhigh",
                          service_tier="priority")
        with self.compiler():
            self.run_prepare()
        r = caller.load_request(self.output / "request.json")
        self.assertEqual((r.execution_profile, r.model, r.reasoning_effort, r.service_tier),
                         ("balanced", "gpt-5.6-sol", "xhigh", "priority"))

    def test_action_focused_verifiers_is_rejected_before_local_or_dcf_preparation(self):
        base_action = json.loads(self.action.read_text())
        for managed in (False, True):
            for value in ([], [{"argv": ["/usr/bin/true"]}], None):
                for with_supported in (False, True):
                    action = {**base_action, "focused_verifiers": value}
                    if with_supported:
                        action["verifier_commands"] = [{"argv": ["/usr/bin/true"]}]
                    self.action.write_text(json.dumps(action))
                    with self.subTest(managed=managed, value=value, with_supported=with_supported):
                        with (patch.object(local_context, "dcf_root",
                                           return_value=self.f.workspace if managed else None) as route,
                              patch.object(local_context, "compile_context") as local_compile,
                              patch.object(stack, "_compile_dcf") as dcf_compile,
                              patch.object(stack, "_compile_navigated_dcf") as navigated_compile,
                              self.compiler() as compiler,
                              patch.object(caller, "decode_request") as decode,
                              patch.object(caller, "preflight") as preflight,
                              patch.object(caller, "execute") as execute,
                              patch.object(caller, "_write_create_only") as publish):
                            with self.assertRaisesRegex(caller.EmbeddedNokiyError,
                                    r"action\.focused_verifiers is unsupported; use action\.verifier_commands") as error:
                                self.run_prepare("research_execution_engine" if managed else None)
                        self.assertEqual(error.exception.code, "NOKIY_FULL_STACK_PREPARATION_FAILED")
                        for downstream in (route, local_compile, dcf_compile, navigated_compile,
                                           compiler, decode, preflight, execute, publish):
                            downstream.assert_not_called()
                        self.assertFalse(self.output.exists())

    def test_dcf_requested_verifier_is_compiled_with_request_artifacts(self):
        action = json.loads(self.action.read_text())
        action["verifier_commands"] = [{"argv": ["/usr/bin/true"]}]
        self.compiled["contract"].update(
            verifier_commands=action["verifier_commands"],
            verifier_artifact_root=self.draft["artifact_root"],
            focused_verifiers=[{"declared": True, "result_status": "pass"}])
        for explicit_root in (False, True):
            with self.subTest(explicit_root=explicit_root):
                if explicit_root:
                    action["verifier_artifact_root"] = self.draft["artifact_root"]
                self.action.write_text(json.dumps(action))
                self.output = self.f.root / ("prepared-matching" if explicit_root
                                            else "prepared-omitted")
                with (self.compiler() as compiler,
                      patch.object(caller, "preflight", return_value={"status": "READY"}),
                      patch.object(caller, "execute") as execute):
                    result = self.run_prepare()
                execute.assert_not_called()
                self.assertEqual(compiler.call_count, 1)
                self.assertEqual(result["status"], "PREPARED")
                self.assertFalse(result["provider_execution_started"])
                self.assertTrue((self.output / "request.json").is_file())
                self.assertTrue((self.output / "preparation.json").is_file())
                submitted = json.loads(compiler.call_args_list[0].kwargs["input"])
                self.assertEqual(submitted["verifier_artifact_root"], self.draft["artifact_root"])
                self.assertEqual(submitted["verifier_commands"], action["verifier_commands"])
                contract = json.loads((self.output / "jspace.json").read_text())
                self.assertEqual(contract["verifier_artifact_root"], self.draft["artifact_root"])
                self.assertEqual(contract["verifier_commands"], action["verifier_commands"])
                self.assertEqual(contract["focused_verifiers"], self.compiled["contract"]["focused_verifiers"])

    def test_dcf_compiler_cannot_silently_drop_requested_verifier(self):
        action = json.loads(self.action.read_text())
        action["verifier_commands"] = [{"argv": ["/usr/bin/true"]}]
        self.action.write_text(json.dumps(action))
        with self.compiler(), patch.object(caller, "preflight") as preflight:
            with self.assertRaises(caller.EmbeddedNokiyError) as error:
                self.run_prepare()
        self.assertEqual(error.exception.code, "NOKIY_FULL_STACK_VERIFIER_UNBOUND")
        preflight.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_dcf_verifier_artifact_root_must_match_request(self):
        action = json.loads(self.action.read_text())
        action.update(verifier_commands=[{"argv": ["/usr/bin/true"]}],
                      verifier_artifact_root=str(self.f.root / "other-artifacts"))
        self.action.write_text(json.dumps(action))
        with (self.compiler() as compiler,
              patch.object(caller, "preflight") as preflight,
              patch.object(caller, "execute") as execute):
            with self.assertRaises(caller.EmbeddedNokiyError) as error:
                self.run_prepare()
        blocker = "NOKIY_FULL_STACK_VERIFIER_ARTIFACT_ROOT_MISMATCH"
        self.assertEqual(error.exception.code, blocker)
        self.assertEqual(error.exception.detail, "verifier artifact root differs from request")
        failure = caller._failure(error.exception)
        self.assertEqual(failure["first_typed_blocker"], blocker)
        self.assertEqual(failure["detail_sha256"],
                         hashlib.sha256(b"verifier artifact root differs from request").hexdigest())
        compiler.assert_not_called()
        preflight.assert_not_called()
        execute.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_exact_dcf_navigation_adds_locator_without_widening_action(self):
        target = "symbol:scripts.ops.dcf.jspace.render_task_context_capsule"
        generation = self.compiled["contract"]["dcf_generation"]["generation_id"]
        action = {"mission": {"task_id": "test"}, "operations": ["read"],
                  "read_scopes": ["scripts/ops/dcf/jspace.py"],
                  "context_summary": "Inspect the admitted function.",
                  "navigation_targets": [target]}
        self.action.write_text(json.dumps(action))
        navigation = {"capability_id": "source-navigation", "target": target,
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": generation,
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": target, "payload": {
                              "path": "scripts/ops/dcf/jspace.py", "line": 1233}}]}}
        compiled = self.compiled_navigation(action, navigation)
        responses = [subprocess.CompletedProcess([], 0, json.dumps(compiled), "")]
        with patch.object(stack.subprocess, "run", side_effect=responses) as run:
            prepared = self.run_prepare()
        self.assertEqual(len(run.call_args_list), 1)
        self.assertTrue(run.call_args.args[0][-1].endswith("dcf_prepare.py"))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["action"], action)
        compiled_action = json.loads((self.output / "preparation.json").read_text())["action"]
        self.assertNotIn("navigation_targets", compiled_action)
        self.assertEqual(compiled_action["read_scopes"], action["read_scopes"])
        self.assertIn("scripts/ops/dcf/jspace.py:1233", compiled_action["context_summary"])
        self.assertEqual(prepared["navigation_locators"][0]["generation_id"], generation)

    def navigation_pages(self, target="symbol:scripts.ops.dcf.jspace.render_task_context_capsule"):
        action = {"mission": {"task_id": "test"}, "operations": ["read"],
                  "read_scopes": ["scripts/ops/dcf/jspace.py"], "write_scopes": [],
                  "context_summary": "Inspect the admitted function.", "navigation_targets": [target]}
        symbol = {"entity_id": target, "payload": {"path": "scripts/ops/dcf/jspace.py", "line": 1233}}
        envelope = {"capability_id": "source-navigation", "target": target,
                    "freshness_status": "current", "domain_verdict": "pass", "projection_status": "pass",
                    "generation_id": self.compiled["contract"]["dcf_generation"]["generation_id"]}
        initial = dict(envelope, result={
            "resolved": [symbol], "graph": {"nodes": ["unrelated graph preview"]},
            "_projection": {"complete": False, "full_result_sha256": "a" * 64,
                            "page_offset": 0, "returned_item_count": 1, "next_offset": 1}})
        selection = dict(envelope, result={
            "result_pointer": "/resolved", "items": [symbol], "item_count": 1,
            "_projection": {"complete": True, "full_result_sha256": "a" * 64,
                            "page_offset": 0, "returned_item_count": 1, "next_offset": None,
                            "omitted_item_count": 0, "deferred_item_count": 0}})
        return action, initial, selection

    def navigate_action(self, action):
        return stack._navigate_known_symbols(
            self.f.workspace, self.f.workspace / "scripts/ops/dcf.py",
            self.f.workspace / ".venv/bin/python", action)

    def test_paginated_dcf_navigation_prepares_only_same_locator_context(self):
        action, initial, selection = self.navigation_pages()
        original = json.loads(json.dumps(action))
        complete = json.loads(json.dumps(initial))
        complete["result"]["_projection"]["complete"] = True
        with patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(complete), "")) as fast_run:
            fast_action, fast_locators = self.navigate_action(action)
        self.assertEqual(fast_run.call_count, 1)
        self.action.write_text(json.dumps(action))
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(reply), "")
                for reply in (initial, selection, self.compiled)]) as run:
            amended, locators = self.navigate_action(action)
            stack._compile_dcf(self.f.workspace, self.f.workspace / "scripts/ops/dcf.py",
                               self.f.workspace / ".venv/bin/python", "fixture", amended)
        self.assertEqual(run.call_count, 3)  # two navigation queries, one compile
        argv = [str(self.f.workspace / ".venv/bin/python"), "-B",
                str(self.f.workspace / "scripts/ops/dcf.py"), "query", "--capability", "source-navigation",
                "--target", initial["target"], "--depth", "1", "--json"]
        self.assertEqual(run.call_args_list[0].args[0], argv)
        self.assertEqual(run.call_args_list[1].args[0], argv + [
            "--result-pointer", "/resolved", "--expected-generation-id", initial["generation_id"],
            "--expected-full-result-sha256", "a" * 64])
        for call in run.call_args_list[:2]:
            self.assertEqual(call.kwargs, {"cwd": self.f.workspace, "capture_output": True,
                                           "text": True, "timeout": 15})
        compiled_action = json.loads(run.call_args_list[2].kwargs["input"])
        expected = dict(action)
        expected.pop("navigation_targets")
        expected["context_summary"] += (
            "\nDCF source-navigation locators (context only; no read/write grant):\n"
            + initial["target"] + " -> scripts/ops/dcf/jspace.py:1233")
        self.assertEqual(compiled_action, expected)
        self.assertEqual(compiled_action, fast_action)  # same grants and context bytes, no graph/binding injection
        self.assertEqual(locators, fast_locators)
        self.assertEqual(action, original)
        self.assertEqual(amended, expected)

    def test_paginated_navigation_does_not_need_resolved_in_graph_preview(self):
        for preview in ([], None):
            with self.subTest(preview=preview):
                action, initial, selection = self.navigation_pages()
                if preview is None:
                    initial["result"].pop("resolved")
                else:
                    initial["result"]["resolved"] = preview
                with patch.object(stack.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, json.dumps(reply), "")
                        for reply in (initial, selection)]) as run:
                    _, locators = self.navigate_action(action)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(locators[0]["target"], initial["target"])

    def assert_navigation_mutations_rejected(self, page_index, mutations):
        for field, value in mutations:
            with self.subTest(page=page_index, field=field, value=value):
                action, initial, selection = self.navigation_pages()
                pages = [initial, selection][:page_index + 1]
                # Copy because the preview and selection share their symbol fixture.
                pages = json.loads(json.dumps(pages))
                node = pages[page_index]
                parts = field.split(".")
                for part in parts[:-1]:
                    node = node[int(part)] if isinstance(node, list) else node[part]
                if value is ...:
                    node.pop(parts[-1])
                else:
                    node[parts[-1]] = value
                with patch.object(stack.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, json.dumps(reply), "") for reply in pages]) as run:
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                        self.navigate_action(action)
                self.assertEqual(run.call_count, page_index + 1)

    def test_incomplete_navigation_rejects_invalid_binding_without_continuation(self):
        self.assert_navigation_mutations_rejected(0, [
            ("capability_id", "surface-map"), ("target", "symbol:other.item"),
            ("freshness_status", "stale"), ("domain_verdict", "fail"), ("projection_status", "fail"),
            ("generation_id", ...), ("generation_id", None), ("generation_id", ""),
            ("generation_id", 1), ("generation_id", " "), ("generation_id", "gen\n1"),
            ("generation_id", "gen\x00"), ("generation_id", "--option"), ("generation_id", "g" * 256),
            ("result", None), ("result._projection", []),
            ("result._projection.complete", ...), ("result._projection.complete", None),
            ("result._projection.complete", 0),
            ("result._projection.full_result_sha256", ...), ("result._projection.full_result_sha256", None),
            ("result._projection.full_result_sha256", ""), ("result._projection.full_result_sha256", "a" * 63),
            ("result._projection.full_result_sha256", "z" * 64),
        ])

    def test_navigation_selection_rejects_unbound_or_partial_locators(self):
        self.assert_navigation_mutations_rejected(1, [
            ("capability_id", "surface-map"), ("target", "symbol:other.item"), ("target", ...),
            ("freshness_status", "stale"), ("domain_verdict", "fail"), ("projection_status", "fail"),
            ("generation_id", "other-generation"), ("generation_id", ...), ("generation_id", None),
            ("result", []), ("result.result_pointer", ...), ("result.result_pointer", "/resolved/0"),
            ("result.result_pointer", "/graph"), ("result.items", ...), ("result.items", []),
            ("result.items", [{}, {}]), ("result.items", {}), ("result.items", [None]),
            ("result.item_count", ...), ("result.item_count", 0), ("result.item_count", 2),
            ("result.item_count", True), ("result.item_count", 1.0), ("result.item_count", "1"),
            ("result._projection", None), ("result._projection.complete", ...),
            ("result._projection.complete", False), ("result._projection.complete", 1),
            ("result._projection.full_result_sha256", ...),
            ("result._projection.full_result_sha256", "b" * 64),
            ("result._projection.page_offset", ...), ("result._projection.page_offset", 1),
            ("result._projection.page_offset", False), ("result._projection.page_offset", 0.0),
            ("result._projection.returned_item_count", ...), ("result._projection.returned_item_count", 0),
            ("result._projection.returned_item_count", 2), ("result._projection.returned_item_count", True),
            ("result._projection.next_offset", ...), ("result._projection.next_offset", 1),
            ("result._projection.deferred_item_refs", [{"pointer": "/resolved/0"}]),
            ("result._projection.deferred_value_refs", [{"pointer": "/resolved/0/payload"}]),
            ("result._projection.deferred_item_refs", None),
            ("result._projection.deferred_value_refs", {}),
            ("result._projection.omitted_item_count", 1), ("result._projection.deferred_item_count", 1),
            ("result._projection.omitted_item_count", None), ("result._projection.deferred_item_count", True),
            ("result.items.0.entity_id", "symbol:other.item"), ("result.items.0.payload", {}),
            ("result.items.0.payload.path", "/absolute.py"), ("result.items.0.payload.path", "../outside.py"),
            ("result.items.0.payload.path", "bad\npath.py"), ("result.items.0.payload.path", ""),
            ("result.items.0.payload.path", None), ("result.items.0.payload.line", ...),
            ("result.items.0.payload.line", 0), ("result.items.0.payload.line", -1),
            ("result.items.0.payload.line", True), ("result.items.0.payload.line", 1.0),
        ])

    def test_navigation_transport_failures_never_retry(self):
        failures = [subprocess.CompletedProcess([], 2, "{}", "failed"),
                    subprocess.CompletedProcess([], 0, "é" * 32769, ""),
                    subprocess.CompletedProcess([], 0, "not json", ""),
                    subprocess.CompletedProcess([], 0, "null", ""),
                    subprocess.CompletedProcess([], 0, "[]", ""),
                    OSError("unavailable"), subprocess.TimeoutExpired("dcf", 15)]
        for page_index in (0, 1):
            for failure in failures:
                with self.subTest(page=page_index, failure=failure):
                    action, initial, _ = self.navigation_pages()
                    responses = ([subprocess.CompletedProcess([], 0, json.dumps(initial), "")]
                                 if page_index else []) + [failure]
                    with patch.object(stack.subprocess, "run", side_effect=responses) as run:
                        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                            self.navigate_action(action)
                    self.assertEqual(run.call_count, page_index + 1)

    def test_paginated_navigation_preserves_cross_target_generation_check(self):
        action, first, _ = self.navigation_pages()
        first["result"]["_projection"]["complete"] = True
        _, second, _ = self.navigation_pages("symbol:other.item")
        second["generation_id"] = "other-generation"
        action["navigation_targets"].append(second["target"])
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(reply), "")
                for reply in (first, second)]) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.navigate_action(action)
        self.assertEqual(run.call_count, 2)  # drift blocks even the bound selection

    def test_paginated_navigation_preserves_compile_generation_check(self):
        action, initial, selection = self.navigation_pages()
        initial["generation_id"] = selection["generation_id"] = "old-generation"
        self.action.write_text(json.dumps(action))
        compiled = self.compiled_navigation(action, initial)
        with patch.object(stack.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 0, json.dumps(compiled), "")) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.run_prepare()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.output.exists())

    def surface_action(self, paths=None):
        paths = paths or ["src/one.py"]
        for name in paths:
            target = self.f.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# source\n")
        action = {"mission": {"task_id": "test"}, "operations": ["read"],
                  "read_scopes": [paths[0]], "context_summary": "Inspect bounded inputs.",
                  "surface_targets": paths}
        self.action.write_text(json.dumps(action))
        return action

    def surface_response(self, paths, matches, **changes):
        wire = {"schema_version": "dcf_query_response_v2", "capability_id": "surface-map",
                "target": None, "depth": 2, "freshness_status": "current",
                "domain_verdict": "pass", "projection_status": "pass",
                "generation_id": self.compiled["contract"]["dcf_generation"]["generation_id"],
                "safety": {"authority_effect": "none", "no_apply": True,
                           "protected_mutation_authorized": False,
                           "command_queue_authority": "proposal_only"},
                "result": {"surface_count": 3, "full_result_sha256": "a" * 64,
                           "paths": paths, "matches": matches, "complete": True}}
        wire.update(changes)
        return wire

    def test_surface_unique_omitted_id_is_diagnostic_only(self):
        paths = ["src/one.py", "src/two.py"]
        action = self.surface_action(paths)
        wire = self.surface_response(paths, [["other", "fixture"], ["fixture"]])
        replies = [subprocess.CompletedProcess([], 0, json.dumps(wire), ""),
                   subprocess.CompletedProcess([], 0, json.dumps(self.compiled), "")]
        self.f.request_path.write_text(json.dumps(self.draft))
        with (patch.object(stack.subprocess, "run", side_effect=replies) as run,
              patch.object(caller, "execute") as execute):
            result = stack.prepare_request(self.f.request_path, self.action, None, self.output)
        execute.assert_not_called()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][:3],
                         [str(self.f.workspace / ".venv/bin/python"), "-B", "-c"])
        self.assertIn("query_capability", run.call_args_list[0].args[0][3])
        self.assertIn("path_matches_surface", run.call_args_list[0].args[0][3])
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 30)
        self.assertEqual(run.call_args_list[0].kwargs["input"], json.dumps(paths))
        self.assertEqual(run.call_args_list[1].args[0][run.call_args_list[1].args[0].index("--surface-id") + 1],
                         "fixture")
        compiled_action = json.loads(run.call_args_list[1].kwargs["input"])
        self.assertNotIn("surface_targets", compiled_action)
        self.assertEqual(compiled_action["read_scopes"], action["read_scopes"])
        self.assertEqual(compiled_action["operations"], ["read"])
        self.assertEqual(compiled_action["context_summary"], action["context_summary"])
        self.assertEqual(result["surface_resolution"]["common_surface_ids"],
                         ["fixture"])
        self.assertNotIn("surface_resolution", (self.output / "capsule.json").read_text())

    def test_surface_explicit_match_and_no_legacy_lookup(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        wire = self.surface_response(paths, [["other", "fixture"]])
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(wire), ""),
                subprocess.CompletedProcess([], 0, json.dumps(self.compiled), "")]) as run:
            result = self.run_prepare("fixture")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["surface_resolution"]["selected_surface_id"],
                         "fixture")

    def test_surface_targets_do_not_modify_write_or_operation_declarations(self):
        paths = ["src/one.py"]
        action = self.surface_action(paths)
        action.update(write_scopes=["answer.txt"], declared_targets=["answer.txt"],
                      operations=["read", "modify"])
        wire = self.surface_response(paths, [["fixture"]])
        with patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(wire), "")) as run:
            compiled, surface_id, evidence, _ = stack._resolve_surface_targets(
                self.f.workspace, self.f.workspace / ".venv/bin/python", action, "fixture")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(surface_id, "fixture")
        self.assertEqual(evidence["targets"], paths)
        self.assertEqual(compiled, {key: value for key, value in action.items()
                                    if key != "surface_targets"})

    def test_surface_lookup_and_symbol_navigation_share_compiled_generation(self):
        paths = ["src/one.py"]
        action = self.surface_action(paths)
        action["navigation_targets"] = ["symbol:package.item"]
        self.action.write_text(json.dumps(action))
        generation = self.compiled["contract"]["dcf_generation"]["generation_id"]
        navigation = {"capability_id": "source-navigation", "target": "symbol:package.item",
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": generation,
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": "symbol:package.item", "payload": {
                              "path": "src/one.py", "line": 1}}]}}
        replies = [self.surface_response(paths, [["fixture"]]), self.compiled_navigation(action, navigation)]
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(reply), "") for reply in replies]) as run:
            result = self.run_prepare(None)
        self.assertEqual(run.call_count, 2)
        compiled_action = json.loads((self.output / "preparation.json").read_text())["action"]
        self.assertNotIn("surface_targets", compiled_action)
        self.assertNotIn("navigation_targets", compiled_action)
        self.assertEqual(compiled_action["read_scopes"], action["read_scopes"])
        self.assertEqual(result["navigation_locators"][0]["generation_id"], generation)

    def test_surface_navigation_generation_disagreement_blocks_compilation(self):
        paths = ["src/one.py"]
        action = self.surface_action(paths)
        action["navigation_targets"] = ["symbol:package.item"]
        self.action.write_text(json.dumps(action))
        navigation = {"capability_id": "source-navigation", "target": "symbol:package.item",
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": "other-generation",
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": "symbol:package.item", "payload": {
                              "path": "src/one.py", "line": 1}}]}}
        replies = [self.surface_response(paths, [["fixture"]]), self.compiled_navigation(action, navigation)]
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(reply), "") for reply in replies]) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.run_prepare(None)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(self.output.exists())

    def test_surface_resolution_failures_do_not_compile_or_send(self):
        paths = ["src/one.py", "src/two.py"]
        self.surface_action(paths)
        cases = [
            ([[], ["one"]], {}, "SURFACE_UNRESOLVED", None),
            ([["one"], ["two"]], {}, "SURFACE_UNRESOLVED", None),
            ([["one", "two"], ["one", "two"]], {}, "SURFACE_AMBIGUOUS", None),
            ([["one"], ["one"]], {}, "SURFACE_MISMATCH", "wrong"),
            ([["one"], ["one"]], {"freshness_status": "stale"}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"domain_verdict": "blocked"}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"generation_id": ""}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"result": {"complete": False}}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"result": {"complete": True, "paths": paths,
              "matches": [["one"], ["one"]], "surface_count": 3,
              "full_result_sha256": "bad"}}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"result": {"complete": True, "paths": [paths[0]],
              "matches": [["one"], ["one"]], "surface_count": 3,
              "full_result_sha256": "a" * 64}}, "SURFACE_UNVERIFIED", None),
        ]
        for i, (matches, changes, error, explicit) in enumerate(cases):
            with self.subTest(case=i):
                wire = self.surface_response(paths, matches, **changes)
                self.f.request_path.write_text(json.dumps(self.draft))
                with (patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(wire), "")) as run,
                      patch.object(caller, "execute") as execute):
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, error):
                        stack.prepare_request(self.f.request_path, self.action, explicit,
                                              self.f.root / ("fail-" + str(i)))
                self.assertEqual(run.call_count, 1)
                execute.assert_not_called()

    def test_surface_query_failure_and_output_limit_are_unverified(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        self.f.request_path.write_text(json.dumps(self.draft))
        for i, process in enumerate((
                subprocess.CompletedProcess([], 2, "", "failure"),
                subprocess.CompletedProcess([], 0, "x" * 65537, ""),
                subprocess.CompletedProcess([], 0, "not-json", ""))):
            with self.subTest(case=i), patch.object(stack.subprocess, "run", return_value=process) as run:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                    stack.prepare_request(self.f.request_path, self.action, None,
                                          self.f.root / ("query-fail-" + str(i)))
            self.assertEqual(run.call_count, 1)
        with patch.object(stack.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 30)) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assertEqual(run.call_count, 1)

    def test_surface_generation_drift_blocks_after_compilation(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        wire = self.surface_response(paths, [["fixture"]], generation_id="older")
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(wire), ""),
                subprocess.CompletedProcess([], 0, json.dumps(self.compiled), "")]) as run:
            self.f.request_path.write_text(json.dumps(self.draft))
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(self.output.exists())

    def test_surface_compiler_must_bind_selected_id(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        wire = self.surface_response(paths, [["other"]])
        self.f.request_path.write_text(json.dumps(self.draft))
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(wire), ""),
                subprocess.CompletedProcess([], 0, json.dumps(self.compiled), "")]) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(self.output.exists())

    def test_surface_file_identity_drift_is_unverified(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        wire = self.surface_response(paths, [["fixture"]])
        target = self.f.workspace / paths[0]
        self.f.request_path.write_text(json.dumps(self.draft))
        def query_and_replace(*args, **kwargs):
            target.unlink()
            target.symlink_to(self.f.workspace / "scripts/ops/dcf.py")
            return subprocess.CompletedProcess([], 0, json.dumps(wire), "")
        with patch.object(stack.subprocess, "run", side_effect=query_and_replace) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_invalid_surface_targets_and_local_mode_never_query(self):
        self.surface_action()
        workspace = self.f.workspace
        (workspace / "src/link.py").symlink_to(workspace / "src/one.py")
        (workspace / "src/alias").symlink_to(workspace / "src", target_is_directory=True)
        os.link(workspace / "src/one.py", workspace / "src/hardlink.py")
        bad = [[], ["src/one.py"] * 2, ["src/one.py"] * 5, ["."], ["src"],
               ["/tmp/x"], ["src/../src/one.py"], ["./src/one.py"], ["src//one.py"],
               ["src/*.py"], ["src/link.py"], ["src/alias/one.py"], [".git/config"],
               ["src/missing.py"], [12], ["src/one.py", "src/hardlink.py"]]
        self.f.request_path.write_text(json.dumps(self.draft))
        for i, paths in enumerate(bad):
            with self.subTest(paths=paths):
                self.action.write_text(json.dumps({"mission": {"task_id": "test"},
                                                   "operations": ["read"], "surface_targets": paths}))
                with patch.object(stack.subprocess, "run") as run:
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_TARGET_INVALID"):
                        stack.prepare_request(self.f.request_path, self.action, None,
                                              self.f.root / ("invalid-" + str(i)))
                run.assert_not_called()
        (workspace / "scripts/ops/dcf.py").unlink()
        with patch.object(stack.subprocess, "run") as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_TARGET_INVALID"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        run.assert_not_called()

    def excerpt_inputs(self):
        target = "symbol:scripts.ops.dcf.jspace.render_task_context_capsule"
        generation = self.compiled["contract"]["dcf_generation"]["generation_id"]
        contract = self.compiled["contract"]
        contract["authorization_semantic_sha256"] = contract["semantic_sha256"]
        contract["read_scopes"] = ["scripts/ops/dcf/jspace.py"]
        contract["write_scopes"] = []
        contract["allowed_operations"] = ["read"]
        contract["denied_operations"] = ["command", "create", "modify", "delete"]
        contract["command_templates"] = []
        contract["semantic_sha256"] = caller._canonical_sha256(
            {key: value for key, value in contract.items() if key != "semantic_sha256"})
        self.draft.update(authority_effect="none", require_tool_call=False)
        self.action.write_text(json.dumps({
            "mission": {"mission_id": "fixture-mission", "task_id": "test", "mode": "DELIVERY",
                        "objective": "Inspect source", "current_predicate": "source_unknown"},
            "operations": ["read"],
            "read_scopes": ["scripts/ops/dcf/jspace.py"], "write_scopes": [],
            "context_summary": "Explain the exact definition.",
            "navigation_targets": [target], "include_source_excerpt": True,
        }))
        navigation = {"capability_id": "source-navigation", "target": target,
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": generation,
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": target, "payload": {
                              "path": "scripts/ops/dcf/jspace.py", "line": 1233}}]}}
        excerpt = {"path": "scripts/ops/dcf/jspace.py", "start_line": 1233,
                   "end_line": 1234, "source_sha256": "a" * 64,
                   "text": "def render_task_context_capsule():\n    pass\n"}
        return navigation, excerpt

    def excerpt_process(self, navigation, *, rebind=None):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[-1].endswith("dcf_prepare.py"):
                compiled = self.compiled_navigation(json.loads(kwargs["input"])["action"], navigation)
                return subprocess.CompletedProcess(argv, 0, json.dumps(compiled), "")
            if "source-navigation" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps(navigation), "")
            if "compile" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.compiled), "")
            self.assertEqual(argv[1:3], ["-B", "-c"])
            self.assertIn("compile_task_context_capsule", argv[3])
            self.assertIn("canonical_contract_bytes", argv[3])
            self.assertIn("render_task_context_capsule", argv[3])
            payload = json.loads(kwargs["input"])
            self.assertEqual(payload["contract"], self.compiled["contract"])
            if rebind is not None:
                return rebind(argv, payload)
            capsule = dict(self.compiled["task_context_capsule"])
            capsule.update(mission=payload["action"]["mission"],
                           context_summary=payload["action"]["context_summary"],
                           dcf_generation=payload["contract"]["dcf_generation"],
                           jspace_semantic_sha256=payload["contract"]["authorization_semantic_sha256"])
            capsule["semantic_sha256"] = caller._canonical_sha256(
                {key: value for key, value in capsule.items() if key != "semantic_sha256"})
            return subprocess.CompletedProcess(argv, 0, json.dumps(capsule), "")
        return run, calls

    def test_opt_in_excerpt_rebinds_canonical_capsule_once_without_provider(self):
        navigation, excerpt = self.excerpt_inputs()
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process) as run,
              patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt) as extract,
              patch("codex_collaboration_harness.source_excerpt.still_current", return_value=True),
              patch.object(caller, "preflight", return_value={"status": "READY"}),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        extract.assert_called_once()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sum(argv[-1].endswith("dcf_prepare.py") for argv, _ in calls), 1)
        first_action = json.loads(calls[0][1]["input"])["action"]
        final_action = json.loads(calls[1][1]["input"])["action"]
        self.assertNotIn("include_source_excerpt", first_action)
        self.assertNotIn("Exact J-Space-authorized source excerpt", first_action["context_summary"])
        self.assertIn("Exact J-Space-authorized source excerpt", final_action["context_summary"])
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), self.compiled["contract"])
        self.assertEqual(json.loads((self.output / "capsule.json").read_text())["context_summary"],
                         final_action["context_summary"])
        self.assertEqual(result["source_excerpt"]["source_sha256"], "a" * 64)
        self.assertNotIn("def render_task_context_capsule", json.dumps(result))
        self.assertIn("context_summary_sha256", result["action"])
        self.assertFalse(result["provider_execution_started"])

    def test_opt_in_excerpt_rebinding_failure_or_drift_never_publishes(self):
        for failure in ("compiler", "malformed", "source", "generation", "identity"):
            with self.subTest(failure=failure):
                self.output = self.f.root / ("prepared-" + failure)
                navigation, excerpt = self.excerpt_inputs()
                def rebind(argv, payload):
                    if failure == "compiler":
                        return subprocess.CompletedProcess(argv, 2, "", "invalid contract")
                    if failure == "identity":
                        (self.f.workspace / "scripts/ops/dcf.py").write_text("changed compiler\n")
                    capsule = dict(self.compiled["task_context_capsule"])
                    capsule.update(mission=payload["action"]["mission"],
                                   context_summary=payload["action"]["context_summary"],
                                   dcf_generation=payload["contract"]["dcf_generation"],
                                   jspace_semantic_sha256=payload["contract"]["authorization_semantic_sha256"])
                    if failure == "malformed":
                        capsule["context_summary"] = "wrong excerpt"
                    if failure == "generation":
                        capsule["dcf_generation"] = {"generation_id": "stale"}
                    capsule["semantic_sha256"] = caller._canonical_sha256(
                        {key: value for key, value in capsule.items() if key != "semantic_sha256"})
                    return subprocess.CompletedProcess(argv, 0, json.dumps(capsule), "")
                run_process, calls = self.excerpt_process(navigation, rebind=rebind)
                current = iter([True, False] if failure == "source" else [True, True])
                with (patch.object(stack.subprocess, "run", side_effect=run_process),
                      patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt),
                      patch("codex_collaboration_harness.source_excerpt.still_current",
                            side_effect=lambda *_: next(current)),
                      patch.object(caller, "preflight", return_value={"status": "READY"})):
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SOURCE_EXCERPT_UNVERIFIED"):
                        self.run_prepare()
                self.assertEqual(sum(argv[-1].endswith("dcf_prepare.py") for argv, _ in calls), 1)
                self.assertFalse(self.output.exists())

    def test_omitted_excerpt_flag_automatically_includes_support(self):
        navigation, excerpt = self.excerpt_inputs()
        action = json.loads(self.action.read_text())
        action.pop("include_source_excerpt")
        self.action.write_text(json.dumps(action))
        excerpt["supporting_spans"] = []
        run_process, _ = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt) as extract,
              patch("codex_collaboration_harness.source_excerpt.still_current", return_value=True),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(extract.call_args.kwargs, {"include_dependencies": True})
        self.assertEqual(result["source_excerpt_decision"], "auto")
        self.assertIn('"supporting_spans": []',
                      json.loads((self.output / "capsule.json").read_text())["context_summary"])
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), self.compiled["contract"])

    def test_explicit_false_does_not_extract_or_rebind(self):
        navigation, _ = self.excerpt_inputs()
        action = json.loads(self.action.read_text())
        action["include_source_excerpt"] = False
        self.action.write_text(json.dumps(action))
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch("codex_collaboration_harness.source_excerpt.extract") as extract,
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        extract.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["source_excerpt_decision"], "disabled")
        self.assertNotIn("source_excerpt", result)

    def test_auto_excerpt_falls_back_only_on_capacity(self):
        from codex_collaboration_harness.source_excerpt import ExcerptBudgetExceeded
        for capacity in (True, False):
            self.output = self.f.root / f"auto-failure-{capacity}"
            navigation, _ = self.excerpt_inputs()
            action = json.loads(self.action.read_text())
            action.pop("include_source_excerpt")
            self.action.write_text(json.dumps(action))
            run_process, calls = self.excerpt_process(navigation)
            error = (ExcerptBudgetExceeded if capacity else caller.EmbeddedNokiyError)(
                "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "capacity or integrity")
            with (patch.object(stack.subprocess, "run", side_effect=run_process),
                  patch("codex_collaboration_harness.source_excerpt.extract", side_effect=error),
                  patch.object(caller, "preflight", return_value={"status": "READY"})):
                if capacity:
                    result = self.run_prepare()
                    self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
                    self.assertNotIn("source_excerpt", result)
                    self.assertEqual(json.loads((self.output / "jspace.json").read_text()),
                                     self.compiled["contract"])
                else:
                    with self.assertRaises(caller.EmbeddedNokiyError):
                        self.run_prepare()
                    self.assertFalse(self.output.exists())
            self.assertEqual(len(calls), 1)

    def test_auto_excerpt_eligibility_does_not_change_task_requirements(self):
        import copy
        self.excerpt_inputs()
        original = copy.deepcopy(self.compiled["contract"])
        locators = [{"path": original["read_scopes"][0]}]
        self.assertTrue(stack._auto_excerpt_eligible(self.draft, original, locators))
        for update in ({"require_tool_call": True}, {"require_tool_call": None},
                       {"authority_effect": "workspace"}):
            self.assertFalse(stack._auto_excerpt_eligible({**self.draft, **update}, original, locators))
        for update in ({"read_scopes": ["scripts/**"]}, {"write_scopes": ["src/a.py"]},
                       {"allowed_operations": ["read", "modify"]},
                       {"allowed_operations": ["read", "command"]},
                       {"denied_operations": ["read"]}, {"command_templates": ["pytest"]},
                       {"verifier_commands": [{"id": "verify"}]}):
            self.assertFalse(stack._auto_excerpt_eligible(self.draft, {**original, **update}, locators))
        self.assertFalse(stack._auto_excerpt_eligible(self.draft, original, []))
        self.assertFalse(stack._auto_excerpt_eligible(self.draft, original, locators * 2))
        admitted = {**original, "allowed_operations": ["read", "command"], "source_read": True}
        self.assertTrue(stack._auto_excerpt_eligible(self.draft, admitted, locators))
        self.assertTrue(stack._auto_excerpt_eligible(self.draft,
            {**admitted, "focused_verifiers": [{"declared": True, "result_status": "pass"}]}, locators))
        self.assertEqual(original, self.compiled["contract"])

    def test_auto_excerpt_json_expansion_falls_back_without_truncation(self):
        navigation, excerpt = self.excerpt_inputs()
        action = json.loads(self.action.read_text())
        action.pop("include_source_excerpt")
        self.action.write_text(json.dumps(action))
        excerpt["text"] = "\u00e9" * 6000
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)

    def test_opt_in_excerpt_final_preflight_failure_never_publishes_request(self):
        navigation, excerpt = self.excerpt_inputs()
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt),
              patch("codex_collaboration_harness.source_excerpt.still_current", return_value=True),
              patch.object(caller, "preflight", side_effect=caller.EmbeddedNokiyError(
                  "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "source drift during preflight"))):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SOURCE_EXCERPT_UNVERIFIED"):
                self.run_prepare()
        self.assertEqual(sum(argv[-1].endswith("dcf_prepare.py") for argv, _ in calls), 1)
        self.assertFalse((self.output / "request.json").exists())

    def edit_excerpt_inputs(self):
        authority_effect = self.draft.get("authority_effect")
        navigation, _ = self.excerpt_inputs()
        name = "scripts/ops/dcf/jspace.py"
        source = ("LIMIT = 20\n"
                  "def normalize(value):\n    return min(int(value), LIMIT)\n\n"
                  "def render(items, limit):\n    return items[:normalize(limit)]\n")
        path = self.f.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        contract = self.compiled["contract"]
        contract.update(allowed_operations=["read", "modify", "command"],
                        denied_operations=["create", "delete"], source_read=True,
                        write_scopes=[name])
        contract["semantic_sha256"] = caller._canonical_sha256(
            {key: value for key, value in contract.items() if key != "semantic_sha256"})
        self.draft.update(authority_effect=authority_effect, require_tool_call=True)
        navigation_rows = []
        for symbol, line in (("render", 5), ("normalize", 2)):
            row = json.loads(json.dumps(navigation))
            row["target"] = "symbol:scripts.ops.dcf.jspace." + symbol
            row["result"]["resolved"][0].update(entity_id=row["target"], payload={"path": name, "line": line})
            navigation_rows.append(row)
        action = json.loads(self.action.read_text())
        action.pop("include_source_excerpt")
        action.update(operations=["read", "modify", "command"], write_scopes=[name],
                      context_summary="Edit normalize and render, then read back the changed source.",
                      navigation_targets=[row["target"] for row in navigation_rows])
        self.action.write_text(json.dumps(action))
        return navigation_rows, path

    def test_auto_edit_context_rebinds_once_without_changing_authority_or_tools(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        original = json.loads(json.dumps(self.compiled["contract"]))
        authority_effect = self.draft["authority_effect"]
        run_process, calls = self.excerpt_process(navigation)
        def preflight(request):
            capsule = json.loads((self.output / "capsule.json").read_text())
            source_excerpt.verify_context_excerpt(self.f.workspace, capsule, original)
            self.assertTrue(request.require_tool_call)
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)
        request = json.loads((self.output / "request.json").read_text())
        self.assertIs(request["require_tool_call"], True)
        self.assertEqual(request["authority_effect"], authority_effect)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        excerpt = json.loads(summary.split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual(len(excerpt["files"]), 1)
        text = "".join(span["text"] for span in excerpt["files"][0]["spans"])
        self.assertEqual(text.count("def normalize("), 1)
        self.assertEqual(text.count("def render("), 1)
        self.assertEqual(excerpt["files"][0]["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotIn("def normalize", json.dumps(result))
        self.assertIn("fresh readback", excerpt["notice"])

    def test_auto_edit_changed_source_during_rebind_is_rejected(self):
        navigation, path = self.edit_excerpt_inputs()
        run_process, _ = self.excerpt_process(navigation)
        def change_after_rebind(argv, **kwargs):
            response = run_process(argv, **kwargs)
            if argv[1:3] == ["-B", "-c"]:
                path.write_text(path.read_text().replace("LIMIT = 20", "LIMIT = 30"))
            return response
        with (patch.object(stack.subprocess, "run", side_effect=change_after_rebind),
              patch.object(caller, "preflight") as preflight):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SOURCE_EXCERPT_UNVERIFIED"):
                self.run_prepare()
        preflight.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_auto_edit_preflight_hook_rejects_stale_source(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        run_process, _ = self.excerpt_process(navigation)
        def changed_at_preflight(request):
            path.write_text(path.read_text() + "\nCHANGED = True\n")
            source_excerpt.verify_context_excerpt(
                self.f.workspace, json.loads((self.output / "capsule.json").read_text()),
                self.compiled["contract"])
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=changed_at_preflight)):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "preimage changed"):
                self.run_prepare()
        self.assertFalse((self.output / "request.json").exists())

    def test_auto_edit_large_render_preloads_only_complete_normalize(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text() + "    pass\n" * 120)
        original = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(len(calls), 2)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        excerpt = json.loads(summary.split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual(excerpt["notice"], source_excerpt.PARTIAL_EDIT_NOTICE)
        self.assertEqual(len(excerpt["deferred_locators"]), 1)
        deferred = json.dumps(excerpt["deferred_locators"])
        self.assertIn("scripts/ops/dcf/jspace.py", deferred)
        self.assertTrue('"line": 5' in deferred or ':5' in deferred, deferred)
        text = "".join(span["text"] for file in excerpt["files"] for span in file["spans"])
        self.assertIn("def normalize(value):\n    return min(int(value), LIMIT)", text)
        self.assertNotIn("def render(", text)
        request = json.loads((self.output / "request.json").read_text())
        self.assertIs(request["require_tool_call"], True)
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)

    def test_auto_partial_edit_binds_support_navigation_without_changing_grants(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text().replace(
            "return min(int(value), LIMIT)", "return helper(value)")
            + "    pass\n" * 120 + "\ndef helper(value):\n" + "    pass\n" * 125)
        original = json.loads(json.dumps(self.compiled["contract"]))
        run_process, _ = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        capsule = json.loads((self.output / "capsule.json").read_text())
        excerpt = json.loads(capsule["context_summary"].split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual([row["name"] for row in excerpt["support_navigation"]["locators"]], ["helper"])
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)
        source_excerpt.verify_context_excerpt(self.f.workspace, capsule, original)

    def test_auto_edit_all_definitions_oversized_uses_ordinary_read_fallback(self):
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text().replace(
            "    return min(int(value), LIMIT)",
            "    pass\n" * 121 + "    return min(int(value), LIMIT)") + "    pass\n" * 120)
        navigation[0]["result"]["resolved"][0]["payload"]["line"] += 121
        original = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)
        request = json.loads((self.output / "request.json").read_text())
        self.assertIs(request["require_tool_call"], True)
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)

    def test_auto_edit_support_overflow_rebinds_definitions_only(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text().replace("return items[:normalize(limit)]",
                                                "return helper(items[:normalize(limit)])") +
                        "def helper(items):\n" + "    pass\n" * 241 + "    return items\n")
        contract = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        def preflight(request):
            source_excerpt.verify_context_excerpt(self.f.workspace,
                json.loads((self.output / "capsule.json").read_text()), contract)
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight)):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(len(calls), 2)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        excerpt = json.loads(summary.split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual(excerpt["notice"], source_excerpt.PARTIAL_EDIT_NOTICE)
        self.assertEqual(excerpt["deferred_locators"], [])
        self.assertNotIn("def helper", json.dumps(excerpt["files"]))
        self.assertIs(json.loads((self.output / "jspace.json").read_text())["source_read"], True)

    def test_auto_edit_preloads_multiple_complete_targets_under_fixed_byte_cap(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text().replace("    return min(int(value), LIMIT)",
                                                "    pass\n" * 79 + "    return min(int(value), LIMIT)") +
                        "    pass\n" * 79)
        navigation[0]["result"]["resolved"][0]["payload"]["line"] += 79
        contract = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        def preflight(request):
            source_excerpt.verify_context_excerpt(self.f.workspace,
                json.loads((self.output / "capsule.json").read_text()), contract)
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(len(calls), 2)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        excerpt = json.loads(summary.split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual(excerpt["line_budget"], 240)
        self.assertNotIn("deferred_locators", excerpt)
        text = "".join(span["text"] for row in excerpt["files"] for span in row["spans"])
        self.assertEqual(text.count("def normalize("), 1)
        self.assertEqual(text.count("def render("), 1)
        self.assertLessEqual(len(text.encode()), source_excerpt.MAX_EXCERPT_BYTES)
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), contract)
        self.assertNotIn("def normalize", json.dumps(result))

    def test_auto_edit_envelope_allocation_fits_four_complete_targets_above_raw_cap(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        text = "LIMIT = 20\n"
        rows = []
        definitions = []
        for symbol in ("normalize", "render", "select", "summarize"):
            row = json.loads(json.dumps(navigation[0]))
            row["target"] = "symbol:scripts.ops.dcf.jspace." + symbol
            row["result"]["resolved"][0].update(entity_id=row["target"],
                payload={"path": "scripts/ops/dcf/jspace.py", "line": len(text.splitlines()) + 1})
            rows.append(row)
            definition = (f"def {symbol}():\n    value = '" + "x" * 2700 + "'\n"
                          + "    pass\n" * 76 + "    return value, LIMIT\n")
            definitions.append(definition)
            text += definition + "\n"
        path.write_text(text, encoding="utf-8")
        action = json.loads(self.action.read_text())
        action["navigation_targets"] = [row["target"] for row in rows]
        self.action.write_text(json.dumps(action))
        contract = json.loads(json.dumps(self.compiled["contract"]))
        authority_effect = self.draft["authority_effect"]
        run_process, calls = self.excerpt_process(rows)
        def preflight(request):
            source_excerpt.verify_context_excerpt(self.f.workspace,
                json.loads((self.output / "capsule.json").read_text()), contract)
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(len(calls), 2)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        raw = summary.split(source_excerpt.CONTEXT_MARKER)[1]
        excerpt = json.loads(raw)
        self.assertEqual(excerpt["allocation_mode"], "envelope_bounded")
        self.assertEqual(excerpt["byte_budget"], 24576)
        self.assertEqual(excerpt["line_budget"], 480)
        self.assertNotIn("deferred_locators", excerpt)
        selected = "".join(span["text"] for file in excerpt["files"] for span in file["spans"])
        self.assertGreater(len(selected.encode()), source_excerpt.MAX_EXCERPT_BYTES)
        self.assertLessEqual(len(raw.encode()), 24576)
        for definition in definitions:
            self.assertEqual(selected.count(definition), 1)
        self.assertEqual(selected.count("LIMIT = 20"), 1)
        self.assertEqual(excerpt["files"][0]["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), contract)
        request = json.loads((self.output / "request.json").read_text())
        self.assertIs(request["require_tool_call"], True)
        self.assertEqual(request["authority_effect"], authority_effect)
        self.assertNotIn("def normalize", json.dumps(result))

    def test_auto_edit_serialized_overflow_uses_ordinary_read_fallback(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        text = "def normalize():\n    return '" + "é" * 4100 + "'\n\n"
        text += "def render():\n    return '" + "é" * 4100 + "'\n"
        path.write_text(text, encoding="utf-8")
        navigation[1]["result"]["resolved"][0]["payload"]["line"] = 1
        navigation[0]["result"]["resolved"][0]["payload"]["line"] = 4
        contract = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"}),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)
        capsule = json.loads((self.output / "capsule.json").read_text())
        self.assertNotIn(source_excerpt.CONTEXT_MARKER, capsule["context_summary"])
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), contract)
        self.assertIs(json.loads((self.output / "request.json").read_text())["require_tool_call"], True)

    def test_auto_and_explicit_readonly_preparation_keep_legacy_raw_cap(self):
        from codex_collaboration_harness import source_excerpt
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.output = self.f.root / f"readonly-cap-{explicit}"
                navigation, _ = self.excerpt_inputs()
                name = "scripts/ops/dcf/jspace.py"
                path = self.f.workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def render_task_context_capsule():\n    return '" + "x" * 13000 + "'\n")
                navigation["result"]["resolved"][0]["payload"]["line"] = 1
                if not explicit:
                    action = json.loads(self.action.read_text())
                    action.pop("include_source_excerpt")
                    self.action.write_text(json.dumps(action))
                run_process, calls = self.excerpt_process(navigation)
                with (patch.object(stack.subprocess, "run", side_effect=run_process),
                      patch.object(source_excerpt, "extract_edits") as edits,
                      patch.object(caller, "preflight", return_value={"status": "READY"})):
                    if explicit:
                        with self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
                            self.run_prepare()
                        self.assertFalse((self.output / "request.json").exists())
                    else:
                        result = self.run_prepare()
                        self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
                        self.assertNotIn("source_excerpt", result)
                    edits.assert_not_called()
                self.assertEqual(len(calls), 1)

    def test_auto_edit_ineligible_broad_scope_keeps_ordinary_read_path(self):
        navigation, _ = self.edit_excerpt_inputs()
        self.compiled["contract"]["write_scopes"] = ["scripts/**"]
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "ineligible")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)

    def test_auto_edit_with_additional_task_and_test_reads_keeps_same_grants(self):
        navigation, _ = self.edit_excerpt_inputs()
        contract = self.compiled["contract"]
        contract["read_scopes"] += ["TASK.md", "tests/test_example.py"]
        original = json.loads(json.dumps(contract))
        run_process, _ = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)

    def test_edit_explicit_flags_and_no_tool_requirement_keep_old_behavior(self):
        from codex_collaboration_harness import source_excerpt
        for flag in (False, True, "no-tool"):
            with self.subTest(flag=flag):
                self.output = self.f.root / f"edit-flag-{flag}"
                navigation, _ = self.edit_excerpt_inputs()
                action = json.loads(self.action.read_text())
                if flag == "no-tool":
                    self.draft["require_tool_call"] = False
                else:
                    action["include_source_excerpt"] = flag
                self.action.write_text(json.dumps(action))
                run_process, calls = self.excerpt_process(navigation)
                with (patch.object(stack.subprocess, "run", side_effect=run_process),
                      patch.object(source_excerpt, "extract_edits") as extract,
                      patch.object(caller, "preflight", return_value={"status": "READY"})):
                    if flag is True:
                        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "read-only, no-tool"):
                            self.run_prepare()
                    else:
                        result = self.run_prepare()
                        self.assertNotIn("source_excerpt", result)
                        self.assertEqual(result["source_excerpt_decision"],
                                         "disabled" if flag is False else "ineligible")
                    extract.assert_not_called()
                self.assertEqual(len(calls), 1)

    def test_navigation_generation_drift_blocks_before_request(self):
        target = "symbol:scripts.ops.dcf.jspace.render_task_context_capsule"
        self.action.write_text(json.dumps({"mission": {"task_id": "test"},
                                           "operations": ["read"], "context_summary": "Inspect it.",
                                           "navigation_targets": [target]}))
        navigation = {"capability_id": "source-navigation", "target": target,
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": "old-generation",
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": target, "payload": {
                              "path": "scripts/ops/dcf/jspace.py", "line": 1233}}]}}
        compiled = self.compiled_navigation(json.loads(self.action.read_text()), navigation)
        responses = [subprocess.CompletedProcess([], 0, json.dumps(compiled), "")]
        with patch.object(stack.subprocess, "run", side_effect=responses):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_partial_dcf_navigation_blocks_before_compilation(self):
        target = "symbol:scripts.ops.dcf.jspace.render_task_context_capsule"
        self.action.write_text(json.dumps({"mission": {"task_id": "test"},
                                           "operations": ["read"], "context_summary": "Inspect it.",
                                           "navigation_targets": [target]}))
        partial = {"capability_id": "source-navigation", "target": target,
                   "freshness_status": "current", "domain_verdict": "pass",
                   "projection_status": "pass", "generation_id": "generation-1",
                   "result": {"_projection": {"complete": False}, "resolved": []}}
        with patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(partial), "")) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.run_prepare()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_navigation_never_falls_back_to_non_dcf_context(self):
        self.action.write_text(json.dumps({"mission": {"task_id": "test"},
                                           "operations": ["read"], "context_summary": "Inspect it.",
                                           "navigation_targets": ["symbol:package.item"]}))
        (self.f.workspace / "scripts/ops/dcf.py").unlink()
        with patch.object(stack.subprocess, "run") as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "only in a DCF workspace"):
                self.run_prepare()
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_compiler_failure_no_fallback_or_request(self):
        with patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "{}", "failed")):
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_corrupt_contract_never_publishes_executable_request(self):
        self.compiled["contract"]["semantic_sha256"] = "0" * 64
        with self.compiler():
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
        self.assertFalse((self.output / "request.json").exists())

    def test_reuse_rejected_before_compiler(self):
        with self.compiler():
            self.run_prepare()
        before = (self.output / "request.json").read_bytes()
        with self.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
            compiler.assert_not_called()
        self.assertEqual((self.output / "request.json").read_bytes(), before)

    def test_submitted_request_not_recompiled(self):
        self.draft["context_capsule"] = {"path": "old", "sha256": "0" * 64}
        with self.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
            compiler.assert_not_called()

    def test_wrong_thread_rejected_before_compilation(self):
        self.draft["native_thread_id"] = "019fd83e-861a-7b62-a628-0e0ad2f88a27"
        with self.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
            compiler.assert_not_called()

    def test_cli_prepare_is_available(self):
        args = caller.build_parser().parse_args(["prepare", "--request", "draft.json", "--action", "action.json",
                                                "--surface-id", "s", "--output-dir", "/tmp/new"])
        self.assertEqual(args.command, "prepare")

    def aged_v2_request(self):
        f = embedded_fixtures.EmbeddedNokiyFixture()
        f.setUp()
        self.addCleanup(f.tearDown)
        f._canonical_v2_context()
        context = json.loads(f.context.read_text())
        contract = json.loads(f.jspace.read_text())
        generation = contract['dcf_generation']
        generation['generated_at'] = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        generation['action_freshness'] = {'source_fingerprints': {'surface': 'test'}}
        contract['dcf_generation'] = generation
        context['dcf_generation'] = generation
        contract['content_sha256'] = embedded_fixtures.canonical_sha256(
            {k:v for k,v in contract.items() if k != 'content_sha256'})
        context['semantic_sha256'] = embedded_fixtures.canonical_sha256(
            {k:v for k,v in context.items() if k != 'semantic_sha256'})
        f.context.write_text(json.dumps(context))
        f.jspace.write_text(json.dumps(contract))
        return caller.load_request(f._write_request())

    def test_old_generation_requires_live_fingerprint_verifier(self):
        request = self.aged_v2_request()
        with patch.object(stack, 'verify_action_freshness') as verify:
            caller._verify_context(request)
            verify.assert_called_once()
        with patch.object(stack, 'verify_action_freshness', side_effect=caller.EmbeddedNokiyError('STALE', 'changed')):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, 'STALE'):
                caller._verify_context(request)

    def test_action_scoped_preflight_rechecks_embedded_source(self):
        request = self.aged_v2_request()
        with (patch.object(stack, 'verify_action_freshness'),
              patch('codex_collaboration_harness.source_excerpt.verify_context_excerpt',
                    side_effect=caller.EmbeddedNokiyError('NOKIY_SOURCE_EXCERPT_UNVERIFIED', 'changed')) as verify):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, 'NOKIY_SOURCE_EXCERPT_UNVERIFIED'):
                caller._verify_context(request)
        verify.assert_called_once()

    def test_freshness_verifier_failure_is_not_ignored(self):
        with patch.object(stack.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '', 'stale')):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, 'FRESHNESS_UNVERIFIED'):
                stack.verify_action_freshness(self.f.workspace, {})
