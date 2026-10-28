"""Inspect preparation: OS-sandboxed read-and-record actions without exact grants."""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_full_core as fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core as core
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import inspect_context as inspect


class InspectContextTests(unittest.TestCase):
    def setUp(self):
        self.core = fixtures.FullCoreTests()
        self.core.setUp()
        self.addCleanup(self.core.doCleanups)
        self.f = self.core.f
        self.draft = json.loads(self.f.request_path.read_text())
        for key in ("context_capsule", "jspace_contract", "execution_profile"):
            self.draft.pop(key, None)
        self.action = {"mission": {"mission_id": "inspect", "task_id": "evidence", "mode": "DELIVERY",
            "objective": "Check fixture state and record evidence", "current_predicate": "evidence_recorded"},
            "context_summary": "Read the fixture and record a receipt; no deployment or broker effects.",
            "operations": ["read", "command"], "inspect": {}}
        self.output = self.f.root / "prepared-inspect"

    def prepare(self, output=None, surface_id=None):
        self.f.request_path.write_text(json.dumps(self.draft))
        action_path = self.f.root / "action-inspect.json"
        action_path.write_text(json.dumps(self.action))
        return stack.prepare_request(self.f.request_path, action_path, surface_id, output or self.output)

    def verified(self):
        request = caller.load_request(self.output / "request.json")
        return request, caller._verify_context(request)

    def test_inspect_prepares_a_digest_bound_profile_without_exact_grants(self):
        self.prepare()
        request, (identities, capsule, contract) = self.verified()
        self.assertEqual(identities["context_mode"], inspect.MODE)
        policy = contract["inspect_shell"]
        evidence, scratch, profile = inspect.layout(inspect.action_root(request.artifact_root, self.action))
        self.assertEqual(policy["evidence_root"], str(evidence.resolve()))
        self.assertEqual(inspect.profile_path(contract), profile.resolve())
        self.assertEqual(policy["profile_sha256"], hashlib.sha256(profile.read_bytes()).hexdigest())
        self.assertTrue(scratch.is_dir())
        for field in ("read_scopes", "write_scopes", "declared_targets", "command_templates"):
            self.assertEqual(contract[field], [], field)
        self.assertEqual(contract["allowed_operations"], ["command", "read"])
        text = profile.read_text()
        self.assertEqual(text, inspect.profile_text(request.artifact_root.resolve()))
        self.assertIn("(deny signal) (allow signal (target same-sandbox))", text)
        self.assertIn("(deny file-write*)", text)
        self.assertIn('(subpath "/private/tmp")', text)
        self.assertIn(json.dumps(str(request.artifact_root.resolve())), text)
        # The Playwright cache is the one home-directory write: browser registry and daemon state.
        browsers = Path.home().resolve() / "Library" / "Caches" / "ms-playwright"
        self.assertIn(f"(allow file-write* (subpath {json.dumps(str(browsers))}))", text)
        self.assertEqual(text.count(str(Path.home().resolve())), 1)
        self.assertEqual(policy["network"], "inherit")
        self.assertIn("Inspect shell:", capsule["context_summary"])
        self.assertIn("playwright-cli", capsule["context_summary"])
        _, runtime, _, jspace = core.prepare(request)
        with patch.dict(os.environ, {inspect.FENCE_ENV: policy["profile_sha256"]}):
            env = core._environment(request, runtime, self.f.root / "state", navigation_contract=jspace)
            self.assertEqual(env[inspect.PROFILE_ENV], str(profile.resolve()))
            plain = core._environment(request, runtime, self.f.root / "state")
            self.assertNotIn(inspect.PROFILE_ENV, plain)
        for fence in (None, "0" * 64):
            with patch.dict(os.environ, {} if fence is None else {inspect.FENCE_ENV: fence}):
                if fence is None:
                    os.environ.pop(inspect.FENCE_ENV, None)
                unfenced = core._environment(request, runtime, self.f.root / "state", navigation_contract=jspace)
                self.assertNotIn(inspect.PROFILE_ENV, unfenced, "without the applied fence inspect fails closed")
        prompt = core._provider_prompt("Check the fixture.", jspace)
        self.assertIn("Inspect shell: command_run shell lines of any form", prompt)
        self.assertIn(policy["evidence_root"], prompt)
        self.assertNotIn(policy["profile_sha256"], prompt)
        self.assertIn("Capability-gap handoff", prompt)

    def test_tool_env_points_npm_and_playwright_state_inside_the_fence(self):
        root = self.f.root / "artifacts" / "inspect" / ("0" * 16)
        evidence, scratch, _ = inspect.layout(root)
        env = inspect.tool_env(root)
        self.assertEqual(env["PLAYWRIGHT_MCP_OUTPUT_DIR"], str(evidence / "playwright"))
        self.assertEqual(env["PWTEST_DAEMON_SESSION_DIR"], str(scratch / "playwright-daemon"))
        self.assertEqual(env["npm_config_logs_dir"], str(scratch / "npm-logs"))
        self.assertEqual(env["PLAYWRIGHT_MCP_SANDBOX"], "false")
        self.assertEqual(env["npm_config_update_notifier"], "false")
        # A daemon socket name must still fit the 104-byte macOS limit after its own suffix.
        self.assertTrue(env["PWTEST_SOCKETS_DIR"].startswith("/private/tmp/nkpw-"))
        self.assertLessEqual(len(env["PWTEST_SOCKETS_DIR"]), 32)
        self.assertEqual(env["PWTEST_SOCKETS_DIR"], inspect.tool_env(root)["PWTEST_SOCKETS_DIR"])
        cache = Path(env["npm_config_cache"])
        self.assertEqual(cache.name, "nokiy-inspect-npm")
        self.assertTrue(cache.parent == scratch or cache.is_relative_to("/private/var/folders"))
        with patch.object(inspect.subprocess, "run", side_effect=OSError("no getconf")):
            self.assertEqual(inspect.tool_env(root)["npm_config_cache"], str(scratch / "nokiy-inspect-npm"))

    def test_inspect_needs_no_surface_in_a_dcf_managed_workspace(self):
        (self.f.workspace / "scripts/ops").mkdir(parents=True, exist_ok=True)
        (self.f.workspace / "scripts/ops/dcf.py").write_text("raise SystemExit('never executed')\n")
        self.prepare()
        _, (identities, _, _) = self.verified()
        self.assertEqual(identities["context_mode"], inspect.MODE)

    def command_action(self, *templates, **extra):
        (self.f.workspace / "notes.txt").write_text("fixture notes\n")
        action = {key: value for key, value in self.action.items() if key != "inspect"}
        action.update(read_scopes=["notes.txt"], forbidden_effects=["deployment"], command_templates=[
            {"argv": ["cat", "notes.txt"], "effects": ["read"],
             "targets": [{"operation": "read", "path": "notes.txt", "argv_index": 1}]}, *templates])
        action.update(extra)
        return action

    def test_unadmitted_local_read_commands_run_as_inspect(self):
        self.action = self.command_action({"argv": ["rg", "-n", "fixture", "notes.txt"], "effects": ["read"],
                                           "targets": []})
        result = self.prepare()
        self.assertEqual(result["context_mode"], inspect.MODE)
        self.assertIn({"path": "action", "rule": stack.INSPECT_ROUTE_RULE}, result["input_adaptations"])
        _, (identities, capsule, contract) = self.verified()
        self.assertEqual(identities["context_mode"], inspect.MODE)
        self.assertIn("Requested read-only commands: cat notes.txt; rg -n fixture notes.txt", capsule["context_summary"])
        self.assertIn("Requested read paths: notes.txt", capsule["context_summary"])
        self.assertEqual(capsule["authority"]["forbidden_effects"], ["deployment"])
        self.assertEqual(contract["command_templates"], [])

    def test_command_actions_that_may_write_keep_their_refusal(self):
        rg = {"argv": ["rg", "fixture", "notes.txt"], "effects": ["read"], "targets": []}
        # Unadmitted commands in writing actions now go to the parent. A refused writing
        # action must still retain its refusal rather than run as inspect (V430: delete is
        # supported, so deleting an absent target is the refusal here).
        self.action = self.command_action(rg, operations=["read", "command", "delete"],
                                          write_scopes=["out.txt"], target_paths=["out.txt"])
        output = self.f.root / "refused-writer"
        with patch.object(stack, "_read_commands_as_inspect", wraps=stack._read_commands_as_inspect) as route:
            with self.assertRaises(caller.EmbeddedNokiyError) as caught:
                self.prepare(output=output)
        # The refusal was routable, but an action that may write keeps it.
        self.assertEqual(caught.exception.code, "NOKIY_LOCAL_CONTEXT_INVALID")
        self.assertIn({"path": "action.operations", "reason": "scope_mismatch"}, caught.exception.issues)
        route.assert_called_once()
        self.assertIsNone(stack._read_commands_as_inspect(stack._normalize_action_input(self.action)[0]))
        self.assertFalse(output.exists())
        # A routed action must also carry the complete mission inspect requires.
        partial = self.command_action(rg, mission={"task_id": "evidence"})
        self.assertIsNone(stack._read_commands_as_inspect(stack._normalize_action_input(partial)[0]))

    def test_dcf_unverified_read_commands_run_as_inspect(self):
        (self.f.workspace / "scripts/ops").mkdir(parents=True, exist_ok=True)
        (self.f.workspace / "scripts/ops/dcf.py").write_text("raise SystemExit('never executed')\n")
        jspace = self.f.workspace / "scripts/ops/dcf/jspace.py"
        jspace.parent.mkdir(parents=True)
        jspace.write_text("# synthetic J-Space compiler\n")
        (self.f.workspace / ".venv/bin").mkdir(parents=True, exist_ok=True)
        (self.f.workspace / ".venv/bin/python").write_text("synthetic interpreter")
        self.action = self.command_action({"argv": ["git", "show", "HEAD:notes.txt"], "effects": ["read"],
                                           "targets": []})
        refusal = caller.EmbeddedNokiyError("NOKIY_FULL_STACK_PREPARATION_FAILED", "DCF compilation failed",
                                            diagnostics={"stage": "dcf_compile", "returncode": 2,
                                                         "child_error_code": "JSPACE_COMMAND_EFFECT_UNVERIFIED"})
        with patch.object(stack, "_compile_dcf", side_effect=refusal) as compile_dcf:
            result = self.prepare(surface_id="research_execution_engine")
        compile_dcf.assert_called_once()
        self.assertEqual(result["context_mode"], inspect.MODE)
        self.assertIn({"path": "action", "rule": stack.INSPECT_ROUTE_RULE}, result["input_adaptations"])
        other = caller.EmbeddedNokiyError("NOKIY_FULL_STACK_PREPARATION_FAILED", "DCF compilation failed",
                                          diagnostics={"stage": "dcf_compile", "returncode": 2,
                                                       "child_error_code": "JSPACE_COMMAND_EFFECT_UNDERDECLARED"})
        output = self.f.root / "prepared-underdeclared"
        with patch.object(stack, "_compile_dcf", side_effect=other), self.assertRaises(caller.EmbeddedNokiyError) as caught:
            self.prepare(output=output, surface_id="research_execution_engine")
        self.assertIs(caught.exception, other)
        self.assertFalse(output.exists())

    def test_exact_grants_operations_and_network_are_rejected(self):
        pwd = {"argv": ["pwd"], "effects": ["read"], "targets": []}
        cases = [{"write_scopes": ["input.txt"]}, {"command_templates": [pwd]}, {"read_scopes": ["input.txt"]},
                 {"target_paths": ["input.txt"]}, {"operations": ["modify"]},
                 {"operations": ["read", "command", "modify"]}, {"operations": "read"},
                 {"inspect": {"network": "loopback"}}, {"inspect": True}, {"inspect": {"writes": "all"}}]
        base = dict(self.action)
        for index, change in enumerate(cases):
            with self.subTest(change=change):
                self.action = {**base, **change}
                with self.assertRaises(caller.EmbeddedNokiyError) as raised:
                    self.prepare(self.f.root / f"rejected-{index}")
                self.assertEqual(raised.exception.code, inspect.CODE)

    def test_fields_that_grant_nothing_are_adapted_and_reported(self):
        self.action = {**self.action, "operations": ["read"], "read_scopes": [], "write_scopes": [],
                       "target_paths": [], "command_templates": [], "verifier_commands": None,
                       "source_read": True, "include_source_excerpt": True}
        prepared = self.prepare(surface_id="dashboard_operator_projection")
        rules = {row["path"]: row["rule"] for row in prepared["input_adaptations"]}
        self.assertEqual(rules, {
            "action.command_templates": "empty_grant_omitted", "action.read_scopes": "empty_grant_omitted",
            "action.target_paths": "empty_grant_omitted", "action.verifier_commands": "empty_grant_omitted",
            "action.write_scopes": "empty_grant_omitted", "action.source_read": "inspect_shell_subsumes",
            "action.include_source_excerpt": "inspect_shell_subsumes",
            "action.operations": "inspect_implies_read_command", "surface_id": "inspect_needs_no_surface"})
        self.assertIn("original_action_sha256", prepared)
        request, (identities, _, contract) = self.verified()
        self.assertEqual(identities["context_mode"], inspect.MODE)
        self.assertEqual(contract["allowed_operations"], ["command", "read"])
        for field in ("read_scopes", "write_scopes", "declared_targets", "command_templates"):
            self.assertEqual(contract[field], [], field)
        adapted, _ = inspect.adapt_action(self.action)
        evidence = inspect.layout(inspect.action_root(request.artifact_root, adapted))[0]
        self.assertEqual(contract["inspect_shell"]["evidence_root"], str(evidence.resolve()))

    def test_navigation_grants_still_reject_inspect(self):
        self.action = {**self.action, "navigation_targets": [{"path": "input.txt", "symbol": "main"}]}
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, "NOKIY_FULL_STACK_PREPARATION_FAILED")

    def test_a_widened_profile_is_rejected_before_execution(self):
        self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, _, profile = inspect.layout(inspect.action_root(request.artifact_root, self.action))
        profile.chmod(0o600)
        widened = profile.read_text().replace("(deny file-write*)\n", "")
        self.assertNotEqual(widened, profile.read_text())
        profile.write_text(widened)
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            caller._verify_context(request)
        self.assertEqual(raised.exception.code, "NOKIY_INSPECT_PROFILE_CHANGED")

    def test_each_action_gets_its_own_evidence_root_and_profile(self):
        self.prepare()
        first = caller.load_request(self.output / "request.json")
        first_root = inspect.action_root(first.artifact_root, self.action)
        self.action["context_summary"] = "A second bounded check; record a separate receipt."
        self.output = self.f.root / "second"
        self.prepare()
        _, (_, _, contract) = self.verified()
        second_root = Path(contract["inspect_shell"]["evidence_root"]).parent
        self.assertNotEqual(first_root.resolve(), second_root)
        self.assertEqual((second_root / "profile.sb").read_text(), (first_root / "profile.sb").read_text())


if __name__ == "__main__":
    unittest.main()

