# SPDX-License-Identifier: MIT
"""New-request preparation only; real preflight with a synthetic pinned image."""
import json
import subprocess
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import test_full_core as fixtures
import test_embedded_nokiy as embedded_fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack as stack


class FullStackTests(unittest.TestCase):
    def setUp(self):
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

    def run_prepare(self):
        self.f.request_path.write_text(json.dumps(self.draft))
        return stack.prepare_request(self.f.request_path, self.action, "research_execution_engine", self.output)

    def compiler(self):
        return patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(self.compiled), ""))

    def test_default_direct_full_stack_real_preflight_no_provider(self):
        with self.compiler() as compiler, patch.object(caller, "execute") as execute:
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["status"], "PREPARED")
        self.assertEqual(result["execution_profile"], "direct")
        request = caller.load_request(self.output / "request.json")
        self.assertEqual((request.model, request.reasoning_effort, request.service_tier),
                         ("gpt-6-astra", "high", "default"))
        self.assertEqual(caller.preflight(request)["status"], "READY")
        self.assertIn("--action-stdin", compiler.call_args.args[0])
        self.assertFalse(result["provider_execution_started"])

    def test_explicit_profile_preserved(self):
        self.draft.update(execution_profile="balanced", model="gpt-5.6-sol", reasoning_effort="xhigh",
                          service_tier="priority")
        with self.compiler(): self.run_prepare()
        r = caller.load_request(self.output / "request.json")
        self.assertEqual((r.execution_profile, r.model, r.reasoning_effort, r.service_tier),
                         ("balanced", "gpt-5.6-sol", "xhigh", "priority"))

    def test_compiler_failure_no_fallback_or_request(self):
        with patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "{}", "failed")):
            with self.assertRaises(caller.EmbeddedNokiyError): self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_corrupt_contract_never_publishes_executable_request(self):
        self.compiled["contract"]["semantic_sha256"] = "0" * 64
        with self.compiler():
            with self.assertRaises(caller.EmbeddedNokiyError): self.run_prepare()
        self.assertFalse((self.output / "request.json").exists())

    def test_reuse_rejected_before_compiler(self):
        with self.compiler(): self.run_prepare()
        before = (self.output / "request.json").read_bytes()
        with self.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError): self.run_prepare()
            compiler.assert_not_called()
        self.assertEqual((self.output / "request.json").read_bytes(), before)

    def test_submitted_request_not_recompiled(self):
        self.draft["context_capsule"] = {"path": "old", "sha256": "0" * 64}
        with self.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError): self.run_prepare()
            compiler.assert_not_called()

    def test_wrong_thread_rejected_before_compilation(self):
        self.draft["native_thread_id"] = "019fd83e-861a-7b62-a628-0e0ad2f88a27"
        with self.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError): self.run_prepare()
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
        f.context.write_text(json.dumps(context));f.jspace.write_text(json.dumps(contract))
        return caller.load_request(f._write_request())

    def test_old_generation_requires_live_fingerprint_verifier(self):
        request = self.aged_v2_request()
        with patch.object(stack, 'verify_action_freshness') as verify:
            caller._verify_context(request)
            verify.assert_called_once()
        with patch.object(stack, 'verify_action_freshness', side_effect=caller.EmbeddedNokiyError('STALE', 'changed')):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, 'STALE'):
                caller._verify_context(request)

    def test_freshness_verifier_failure_is_not_ignored(self):
        with patch.object(stack.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '', 'stale')):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, 'FRESHNESS_UNVERIFIED'):
                stack.verify_action_freshness(self.f.workspace, {})
