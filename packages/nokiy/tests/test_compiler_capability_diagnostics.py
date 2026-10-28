# SPDX-License-Identifier: MIT
"""Failure metadata crosses release boundaries without becoming a grant or retry."""
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from codex_collaboration_harness import dcf_prepare as bridge
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack as stack
import test_full_stack


CODE = "JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT"


def metadata():
    return {"capability_id": "verifier-gates", "failed_field": "freshness_status", "observed": "unverified",
            "expected": "current", "generation_id": "generation-a"}


class CapabilityDiagnosticTests(unittest.TestCase):
    def failure(self, diagnostic, code=CODE):
        return {"schema_version": "jspace_error_v1", "status": "blocked", "error_code": code,
                "error": "PRIVATE_RAW_ERROR", "authority_effect": "none", "no_apply": True,
                "capability_diagnostic": diagnostic}

    def test_valid_metadata_crosses_both_prepare_routes_without_raw_text(self):
        for stage in ("dcf_compile", "dcf_navigated_prepare"):
            with self.subTest(stage=stage):
                stdout = json.dumps(self.failure(metadata()))
                completed = subprocess.CompletedProcess([], 2, stdout, "PRIVATE_RAW_ERROR")
                diagnostic = stack._dcf_failure_diagnostics(completed, stage)
                value = caller._failure(caller.EmbeddedNokiyError(
                    "NOKIY_FULL_STACK_PREPARATION_FAILED", "PRIVATE_RAW_ERROR", diagnostics=diagnostic))
                self.assertEqual(value["diagnostics"]["capability_diagnostic"], metadata())
                self.assertEqual(value["diagnostics"]["stdout_sha256"], hashlib.sha256(stdout.encode()).hexdigest())
                self.assertEqual(value["diagnostics"]["child_error_code"], CODE)
                self.assertEqual(value["status"], "BLOCKED_PREEXECUTION")
                self.assertEqual(value["authority_effect"], "none")
                self.assertFalse(value["fallback_used"])
                self.assertNotIn("issues", value)
                self.assertNotIn("PRIVATE_RAW_ERROR", json.dumps(value))

    def test_metadata_is_atomic_closed_and_bound_to_matching_code(self):
        good = metadata()
        invalid = [None, [], {}, {**good, "extra": "PRIVATE_RAW_ERROR"}]
        invalid += [{k: v for k, v in good.items() if k != missing} for missing in good]
        for key, value in (("capability_id", "source-navigation"), ("failed_field", "row.data"),
                           ("observed", "PRIVATE_RAW_ERROR"), ("observed", "current"),
                           ("observed", True), ("expected", "pass"),
                           ("generation_id", "generation\nPRIVATE_RAW_ERROR"),
                           ("generation_id", "/private/secret"), ("generation_id", "a" * 256)):
            invalid.append({**good, key: value})
        for diagnostic in invalid:
            with self.subTest(diagnostic=diagnostic):
                self.assertIsNone(bridge.safe_capability_diagnostic(diagnostic, CODE))
                completed = subprocess.CompletedProcess([], 2, json.dumps(self.failure(diagnostic)), "")
                safe = stack._dcf_failure_diagnostics(completed, "dcf_compile")
                self.assertEqual(safe["child_error_code"], CODE)
                self.assertNotIn("capability_diagnostic", safe)
                self.assertIsNone(caller._safe_preparation_diagnostics({**safe, "capability_diagnostic": diagnostic}))
        for code in ("JSPACE_REQUIRED_CAPABILITY_MISSING", "JSPACE_SOURCE_READ_DENIED", None):
            self.assertIsNone(bridge.safe_capability_diagnostic(good, code))

    def test_status_predicates_use_exact_expected_enum(self):
        for field, observed, expected in (("projection_status", "warn", "pass"),
                                          ("domain_verdict", "not_applicable", "pass"),
                                          ("freshness_status", "missing", "current")):
            value = {**metadata(), "failed_field": field, "observed": observed, "expected": expected}
            self.assertEqual(bridge.safe_capability_diagnostic(value, CODE), value)

    def test_standalone_navigation_bridge_retains_canonical_diagnostic(self):
        class CompileError(ValueError):
            error_code, blocked = CODE, True
            capability_diagnostic = metadata()

        jspace = ModuleType("scripts.ops.dcf.jspace")
        jspace.JSpaceCompileError = CompileError
        runtime = ModuleType("scripts.ops.dcf.runtime")
        runtime.DcfRuntime = lambda root: object()
        output = io.BytesIO()
        with (patch.dict(sys.modules, {jspace.__name__: jspace, runtime.__name__: runtime}),
              patch.object(sys, "path", list(sys.path)),
              patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
                  b'{"surface_id":"fixture","action":{"navigation_targets":["symbol:package.item"]}}'))),
              patch.object(sys, "stdout", SimpleNamespace(buffer=output)),
              patch.object(bridge, "compile_bundle", side_effect=CompileError("PRIVATE_RAW_ERROR")) as compile_bundle):
            self.assertEqual(bridge.main(), 2)
        compile_bundle.assert_called_once()
        value = json.loads(output.getvalue())
        self.assertEqual(value["capability_diagnostic"], metadata())
        self.assertNotIn("error", value)
        self.assertNotIn(b"PRIVATE_RAW_ERROR", output.getvalue())
        safe = stack._dcf_failure_diagnostics(
            subprocess.CompletedProcess([], 2, output.getvalue().decode(), ""), "dcf_navigated_prepare")
        self.assertEqual(safe["capability_diagnostic"], metadata())

    def test_success_and_legacy_code_only_envelopes_are_not_expanded(self):
        value = self.failure(metadata())
        value.pop("capability_diagnostic")
        safe = stack._dcf_failure_diagnostics(subprocess.CompletedProcess([], 2, json.dumps(value), ""), "dcf_compile")
        self.assertNotIn("capability_diagnostic", safe)
        self.assertEqual(safe["child_error_code"], CODE)
        safe = stack._dcf_failure_diagnostics(subprocess.CompletedProcess([], 0, json.dumps(self.failure(metadata())), ""), "dcf_compile")
        self.assertNotIn("child_error_code", safe)
        self.assertNotIn("capability_diagnostic", safe)

    def test_returned_metadata_cannot_mutate_the_original_envelope(self):
        stdout = json.dumps(self.failure(metadata()))
        safe = stack._dcf_failure_diagnostics(subprocess.CompletedProcess([], 2, stdout, ""), "dcf_compile")
        original = copy.deepcopy(safe)
        result = caller._safe_preparation_diagnostics(safe)
        result["capability_diagnostic"]["observed"] = "stale"
        self.assertEqual(safe, original)

    def test_prepare_cli_retains_failure_without_execute_or_fresh_request(self):
        fixture = test_full_stack.FullStackTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # Read-only refusals may prepare as inspect; this writing action must retain
        # the compiler's capability failure without publishing a request.
        fixture.action.write_text(json.dumps({
            "mission": {"mission_id": "capability-diagnostics", "task_id": "prepare", "mode": "DELIVERY",
                "objective": "Prepare a fixture edit with current compiler capabilities",
                "current_predicate": "compiler capability failure is preserved without execution"},
            "context_summary": "Prepare a synthetic edit and retain safe compiler diagnostics on refusal.",
            "operations": ["create", "modify"], "write_scopes": ["answer.txt"], "target_paths": ["answer.txt"]}))
        jspace = fixture.f.workspace / "scripts/ops/dcf/jspace.py"
        jspace.parent.mkdir(parents=True, exist_ok=True)
        jspace.write_text("# synthetic J-Space compiler\n")
        stdout = json.dumps(self.failure(metadata()))
        with (patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, stdout, "")) as run,
              patch.object(caller, "execute") as execute):
            with self.assertRaises(caller.EmbeddedNokiyError) as caught:
                fixture.run_prepare()
        value = caller._failure(caught.exception)
        self.assertEqual(value["diagnostics"]["capability_diagnostic"], metadata())
        execute.assert_not_called()
        run.assert_called_once()
        self.assertFalse(fixture.output.exists())
        output = io.StringIO()
        with patch.object(stack, "prepare_request", side_effect=caught.exception), redirect_stdout(output):
            code = caller.main(["prepare", "--request", "unused", "--action", "unused", "--output-dir", "unused"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["diagnostics"]["capability_diagnostic"], metadata())
