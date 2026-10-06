# SPDX-License-Identifier: MIT
"""Real scoped verifier acceptance, opt-in against an exact candidate router."""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from test_postimage_context import dcf_generation

from codex_collaboration_harness import verifier_parent
from codex_collaboration_harness import embedded_nokiy as caller, full_stack, local_context
from codex_collaboration_harness.graph_process import DarwinProcesses
from codex_collaboration_harness.verifier_parent import ParentVerifier, supervisor_policy


class ParentVerifierDeadlineTests(unittest.TestCase):
    def test_optional_worker_deadline_preserves_individual_verifier_limit(self):
        binding = 'a' * 64
        grant = {'argv': ['/python', '/verify.py'], 'scratch_root': '/scratch', 'timeout_seconds': 7}
        contract = {'authorization_semantic_sha256': binding, 'verifier_commands': [grant]}
        plan = dict(grant, binding=binding, verifier_index=0, profile='unchanged-policy')
        observation = dict(success=True, exit_code=0, stdout='ok', stderr='',
                           process_reaped=True, process_group_empty=True, outcome='known')
        for deadline, expected in ((None, 7), (1000003, 3)):
            with self.subTest(deadline=deadline):
                router = SimpleNamespace(path=Path('/router'), verify=Mock())
                runner = ParentVerifier(Path('/workspace'), contract, router, deadline)
                process, selector = Mock(), MagicMock()
                process.poll.return_value = process.wait.return_value = 0
                for fd, stream in enumerate((process.stdin, process.stdout, process.stderr), 10):
                    stream.fileno.return_value = fd
                    stream.closed = False
                keys = []
                selector.__enter__.return_value = selector
                selector.register.side_effect = lambda stream, event, buffer: keys.append(
                    SimpleNamespace(fd=stream.fileno(), data=buffer, fileobj=stream))
                selector.get_map.side_effect = [{11: True}, {}]
                selector.select.side_effect = lambda _timeout: [(keys[0], 1)]
                with patch.object(verifier_parent.subprocess, 'run',
                                  return_value=SimpleNamespace(stdout=json.dumps(plan).encode())) as planned, \
                        patch.object(verifier_parent.subprocess, 'Popen', return_value=process) as spawn, \
                        patch.object(verifier_parent, 'supervisor_policy',
                                     return_value=('unchanged-policy', Path('/python'), Path('/supervisor'))) as policy, \
                        patch.object(verifier_parent.selectors, 'DefaultSelector', return_value=selector), \
                        patch.object(verifier_parent.os, 'set_blocking'), \
                        patch.object(verifier_parent.os, 'read', return_value=json.dumps(observation).encode()), \
                        patch.object(verifier_parent.time, 'monotonic', return_value=1000000):
                    result = runner(0, 'call', threading.Event())
                self.assertEqual(result, observation)
                self.assertEqual(planned.call_args.kwargs['timeout'], 10 if deadline is None else 3)
                self.assertEqual(float(spawn.call_args.args[0][-1]), expected)
                self.assertTrue(spawn.call_args.kwargs['start_new_session'])
                policy.assert_called_once_with('unchanged-policy')
                router.verify.assert_called_once_with(code='NOKIY_VERIFIER_ROUTER_DRIFT')
                self.assertTrue(runner.cleanup_pass)
                self.assertEqual(runner.calls, 1)

    def test_cancelled_or_expired_parent_rejects_before_planning(self):
        for deadline, cancel in ((None, True), (1, False)):
            with self.subTest(deadline=deadline):
                cancelled = threading.Event()
                if cancel:
                    cancelled.set()
                runner = ParentVerifier(Path('/workspace'),
                    {'authorization_semantic_sha256': 'a' * 64}, Mock(), deadline)
                with patch.object(verifier_parent.subprocess, 'run', side_effect=AssertionError('planned')), \
                        patch.object(verifier_parent.time, 'monotonic', return_value=2):
                    with self.assertRaisesRegex(ValueError, 'VERIFIER_PARENT_CANCELLED'):
                        runner(0, 'call', cancelled)
                self.assertEqual(runner.calls, 0)
                self.assertTrue(runner.cleanup_pass)


class ParentVerifierPostimageTests(unittest.TestCase):
    """Real capture/projection with only the supervisor transport stubbed out."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.source = self.workspace / "work.py"
        self.before = "BASE = 2\ndef helper():\n    return BASE\ndef answer():\n    return helper() + 1\n"
        self.after = self.before.replace("+ 1", "+ 2")
        self.contract = {
            "repo_root": str(self.workspace), "authorization_semantic_sha256": "a" * 64,
            "source_read": True, "allowed_operations": ["command", "read", "modify"],
            "denied_operations": ["create", "delete"],
            "read_scopes": ["work.py"], "write_scopes": ["work.py"],
            "dcf_generation": {"source_snapshot": {"work.py": {
                "sha256": hashlib.sha256(self.before.encode()).hexdigest()}}},
            "verifier_commands": [{"argv": ["/python", "/verify.py"],
                                   "scratch_root": "/scratch", "timeout_seconds": 7}],
        }
        self.observation = dict(success=True, exit_code=0, outcome="known", process_reaped=True,
                                process_group_empty=True, stdout="raw\nstdout\u00e9\x00", stderr="warning\n")
        self.new_runner()

    def new_runner(self):
        self.source.write_bytes(self.before.encode())
        self.router = SimpleNamespace(path=Path("/router"), verify=Mock())
        self.runner = ParentVerifier(self.workspace, self.contract, self.router, None)
        self.assertIsNotNone(self.runner._source_preimages)
        self.source.write_bytes(self.after.encode())

    def invoke(self, observation=None, call_id="call", close_error=False, supervisor_exit=0,
               expected_env=None, during_execution=None):
        observation = self.observation if observation is None else observation
        plan = dict(self.contract["verifier_commands"][0], binding=self.runner.binding,
                    verifier_index=0, profile="unchanged-policy")
        process, selector = Mock(), MagicMock()
        process.poll.return_value = 0
        process.wait.return_value = supervisor_exit
        for fd, stream in enumerate((process.stdin, process.stdout, process.stderr), 10):
            stream.fileno.return_value = fd
            stream.closed = False
        if close_error:
            process.stderr.close.side_effect = OSError("stream close failed")
        keys = []
        selector.__enter__.return_value = selector
        selector.register.side_effect = lambda stream, event, buffer: keys.append(
            SimpleNamespace(fd=stream.fileno(), data=buffer, fileobj=stream))
        selector.get_map.side_effect = [{11: True}, {}]
        selector.select.side_effect = lambda _timeout: [(keys[0], 1)]
        real_read = os.read
        supervisor_output = json.dumps(observation).encode()
        transport_pending = True
        def read(fd, count):
            nonlocal transport_pending
            if transport_pending:
                transport_pending = False
                self.assertEqual(fd, 11)
                if during_execution is not None:
                    during_execution()
                return supervisor_output
            # os is shared with source_excerpt: do not mock real source reads.
            return real_read(fd, count)
        def plan_run(*args, **kwargs):
            if expected_env is not None:
                self.assertEqual(kwargs["env"], {key: value for key, value in expected_env.items()
                                               if key not in {"TMPDIR", "PYTHONPATH"}})
            return SimpleNamespace(stdout=json.dumps(plan).encode())
        with patch.object(verifier_parent.subprocess, "run", side_effect=plan_run), \
                patch.object(verifier_parent.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(verifier_parent, "supervisor_policy",
                             return_value=("unchanged-policy", Path("/python"), Path("/supervisor"))) as policy, \
                patch.object(verifier_parent.selectors, "DefaultSelector", return_value=selector), \
                patch.object(verifier_parent.os, "set_blocking"), \
                patch.object(verifier_parent.os, "read", side_effect=read):
            result = self.runner(0, call_id, threading.Event())
        if expected_env is not None:
            self.assertEqual(spawn.call_args.kwargs["env"], expected_env)
            policy.assert_called_once_with("unchanged-policy")
        return result

    def test_import_roots_set_only_bound_pythonpath_without_ambient_inheritance(self):
        roots = [str(self.workspace / "src"), str(self.workspace / "lib")]
        ambient = {"HOME": "/actual-home", "CODEX_HOME": "/actual-codex-home",
                   "PYTHONPATH": "/ambient", "PYTHONHOME": "/ambient-runtime",
                   "VIRTUAL_ENV": "/ambient-venv", "PATH": "/ambient-bin",
                   "TMPDIR": "/ambient-tmp", "CODEX_THREAD_ID": "not-inherited"}
        for present, codex_home in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(present=present, codex_home=codex_home):
                grant = self.contract["verifier_commands"][0]
                grant.pop("python_import_roots", None)
                if present:
                    grant["python_import_roots"] = roots
                expected = {"HOME": ambient["HOME"], "PATH": "/usr/bin:/bin",
                            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                            "LANG": "C.UTF-8", "TMPDIR": "/scratch"}
                if codex_home:
                    expected["CODEX_HOME"] = ambient["CODEX_HOME"]
                if present:
                    expected["PYTHONPATH"] = os.pathsep.join(roots)
                environment = dict(ambient)
                if not codex_home:
                    environment.pop("CODEX_HOME")
                with patch.dict(os.environ, environment, clear=True):
                    self.assertTrue(self.invoke(expected_env=expected)["success"])
                self.assertTrue(self.runner.cleanup_pass)

    def test_import_root_plan_mismatch_rejects_before_policy_or_spawn(self):
        roots = [str(self.workspace / "src"), str(self.workspace / "lib")]
        field = "python_import_roots"
        cases = [({}, {field: roots}), ({}, {field: []}), ({}, {field: None}),
                 ({field: roots}, {}), ({field: roots}, {field: None}),
                 ({field: roots}, {field: []}), ({field: roots}, {field: roots[::-1]}),
                 ({field: roots}, {field: roots[:1]}), ({field: roots}, {field: ":".join(roots)}),
                 ({field: roots}, {field: roots + [str(self.workspace / "other")]})]
        for bound, echoed in cases:
            with self.subTest(bound=bound, echoed=echoed):
                grant = self.contract["verifier_commands"][0]
                grant.pop(field, None)
                grant.update(bound)
                plan = dict(grant, binding=self.runner.binding, verifier_index=0, profile="unchanged-policy")
                plan.pop(field, None)
                plan.update(echoed)
                with patch.object(verifier_parent.subprocess, "run",
                                  return_value=SimpleNamespace(stdout=json.dumps(plan).encode())), \
                        patch.object(verifier_parent.subprocess, "Popen") as spawn, \
                        patch.object(verifier_parent, "supervisor_policy") as policy:
                    with self.assertRaisesRegex(ValueError, "VERIFIER_PLAN_IDENTITY_MISMATCH"):
                        self.runner(0, "mismatch", threading.Event())
                spawn.assert_not_called()
                policy.assert_not_called()
                self.assertEqual(self.runner.calls, 0)
                self.assertTrue(self.runner.cleanup_pass)

    def dcf_request(self):
        generation = dcf_generation(self.workspace)
        authorization = {
            "schema_version": "jspace_authorization_v1", "repo_root": str(self.workspace),
            "required_domain_bindings": generation["required_domain_bindings"],
            "matched_surface_ids": ["test-surface"], "read_scopes": ["work.py"],
            "write_scopes": ["work.py"], "allowed_operations": ["command", "modify", "read"],
            "denied_operations": ["delete", "install", "network", "system_mutation"],
            "command_templates": [], "declared_targets": ["work.py"], "expansion": {},
            "source_read": True, "verifier_commands": self.contract["verifier_commands"],
            "verifier_artifact_root": str(self.workspace / "artifacts"),
        }
        contract = {key: value for key, value in authorization.items()
                    if key not in {"schema_version", "required_domain_bindings"}}
        contract.update(schema_version="jspace_contract_v2", dcf_generation=generation,
                        authorization_semantic_sha256=caller._canonical_sha256(authorization))
        contract["content_sha256"] = caller._canonical_sha256(contract)
        capsule = {
            "schema_version": "task_context_capsule_v1", "dcf_generation": generation,
            "surface": {"repo_root": str(self.workspace)},
            "jspace_semantic_sha256": contract["authorization_semantic_sha256"],
            "context_summary": "Action-scoped DCF fixture; no synthetic source snapshot.",
        }
        capsule["semantic_sha256"] = caller._canonical_sha256(capsule)
        identities = []
        for name, payload in (("dcf-contract.json", contract), ("dcf-capsule.json", capsule)):
            path = self.workspace / name
            path.write_text(json.dumps(payload))
            identities.append(caller.FileIdentity(path, caller._file_sha256(path)))
        return SimpleNamespace(workspace=self.workspace, jspace_contract=identities[0],
                               context_capsule=identities[1], max_context_age_seconds=1,
                               authority_effect="workspace_artifact", artifact_root=self.workspace / "artifacts")

    def test_validated_real_shape_dcf_without_snapshot_emits_only_after_successful_return(self):
        request = self.dcf_request()
        self.source.write_bytes(self.before.encode())
        with patch.object(full_stack, "verify_action_freshness") as freshness:
            receipt, capsule, contract = caller._verify_context(request)
        freshness.assert_called_once_with(self.workspace, contract)
        self.assertEqual(receipt["context_mode"], "dcf_jspace_required")
        self.assertNotIn("source_snapshot", contract["dcf_generation"])
        original = copy.deepcopy(contract)
        self.contract = contract
        self.runner = ParentVerifier(self.workspace, contract, self.router, None,
                                     validated_dcf_generation=capsule["dcf_generation"])
        self.assertIsNotNone(self.runner._source_preimages)
        self.source.write_bytes(self.after.encode())
        with self.assertRaisesRegex(OSError, "stream close failed"):
            self.invoke(close_error=True)
        self.assertEqual(self.runner._emitted_source_postimages, set())
        for changes in ({"success": False, "exit_code": 1}, {"outcome": "unknown"},
                        {"process_reaped": False}, {"process_group_empty": False}):
            observation = dict(self.observation, **changes)
            self.assertEqual(self.invoke(observation, call_id=str(changes)), observation)
        result = self.invoke(call_id="dcf-success")
        self.assertEqual({key: result[key] for key in self.observation}, self.observation)
        row, = result["source_postimages"]["files"]
        self.assertEqual(row["preimage_sha256"], hashlib.sha256(self.before.encode()).hexdigest())
        self.assertEqual(row["postimage_sha256"], hashlib.sha256(self.after.encode()).hexdigest())
        dedup = self.invoke(call_id="dcf-dedup")
        self.assertEqual({key: dedup[key] for key in self.observation}, self.observation)
        self.assertNotIn("source_postimages", dedup)
        self.assertEqual(dedup["verification_evidence"]["call_id"], "dcf-dedup")
        self.assertEqual(dedup["verification_evidence"]["source_postimages"],
                         result["verification_evidence"]["source_postimages"])
        self.assertEqual(self.contract, original)

    def test_stale_dcf_validation_cannot_reach_preimage_capture(self):
        request = self.dcf_request()
        with patch.object(full_stack, "verify_action_freshness",
                          side_effect=caller.EmbeddedNokiyError("STALE", "required domain changed")), \
                patch.object(verifier_parent.postimage_context, "capture_preimages") as capture:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "STALE"):
                _, capsule, contract = caller._verify_context(request)
                ParentVerifier(self.workspace, contract, self.router, None,
                               validated_dcf_generation=capsule["dcf_generation"])
            capture.assert_not_called()

    def test_dcf_authorization_drift_is_rejected_before_freshness_or_capture(self):
        request = self.dcf_request()
        contract = json.loads(request.jspace_contract.path.read_text())
        contract["read_scopes"].append("ungranted.py")
        contract["content_sha256"] = caller._canonical_sha256(
            {key: value for key, value in contract.items() if key != "content_sha256"})
        request.jspace_contract.path.write_text(json.dumps(contract))
        request.jspace_contract = caller.FileIdentity(request.jspace_contract.path,
            caller._file_sha256(request.jspace_contract.path))
        with patch.object(full_stack, "verify_action_freshness") as freshness, \
                patch.object(verifier_parent.postimage_context, "capture_preimages") as capture:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "semantic digest differs"):
                _, capsule, contract = caller._verify_context(request)
                ParentVerifier(self.workspace, contract, self.router, None,
                               validated_dcf_generation=capsule["dcf_generation"])
            freshness.assert_not_called()
            capture.assert_not_called()

    def test_success_appends_real_context_without_changing_any_verifier_fields(self):
        result = self.invoke()
        self.assertEqual({key: result[key] for key in self.observation}, self.observation)
        self.assertEqual(set(result), set(self.observation) | {"source_postimages"})
        payload = result["source_postimages"]
        self.assertEqual(payload["schema_version"], "nokiy_source_postimages_v1")
        self.assertEqual(payload["jspace_semantic_sha256"], self.runner.binding)
        row, = payload["files"]
        self.assertEqual(row["path"], "work.py")
        self.assertEqual(row["preimage_sha256"], hashlib.sha256(self.before.encode()).hexdigest())
        self.assertEqual(row["postimage_sha256"], hashlib.sha256(self.after.encode()).hexdigest())
        self.assertEqual([item["qualified_name"] for item in row["locators"]], ["answer"])
        text = "".join(span["text"] for span in row["spans"])
        self.assertIn("def helper", text)
        self.assertIn("BASE = 2", text)
        self.assertTrue(self.runner.cleanup_pass)
        self.assertEqual(self.runner.calls, 1)
        self.assertNotIn("source_postimages", self.observation)

    def test_dedup_happens_only_after_return_and_resets_for_a_different_postimage(self):
        first = self.invoke(call_id="first")
        self.assertIn("source_postimages", first)
        self.assertEqual(self.invoke(call_id="same"), self.observation)
        self.source.write_bytes(self.before.replace("+ 1", "+ 3").encode())
        next_result = self.invoke(call_id="new")
        self.assertIn("source_postimages", next_result)
        self.assertEqual(len(self.runner._emitted_source_postimages), 2)
        self.assertEqual(self.runner.calls, 3)

    def test_failed_finalization_does_not_suppress_a_later_successful_return(self):
        projector = verifier_parent.postimage_context.project
        with patch.object(verifier_parent.postimage_context, "project", wraps=projector) as project:
            with self.assertRaisesRegex(OSError, "stream close failed"):
                self.invoke(close_error=True)
            project.assert_not_called()
        self.assertEqual(self.runner._emitted_source_postimages, set())
        self.assertIn("source_postimages", self.invoke(call_id="retry-success"))

    def test_failed_unknown_and_unproven_cleanup_results_never_invoke_projection(self):
        cases = ((dict(success=False, exit_code=1), True),
                 (dict(outcome="unknown"), True), (dict(process_reaped=False), False),
                 (dict(process_group_empty=False), False))
        for changes, cleanup in cases:
            with self.subTest(changes=changes):
                self.new_runner()
                observation = dict(self.observation, **changes)
                with patch.object(verifier_parent.postimage_context, "project") as project:
                    self.assertEqual(self.invoke(observation), observation)
                project.assert_not_called()
                self.assertEqual(self.runner.cleanup_pass, cleanup)
                self.assertEqual(self.runner._emitted_source_postimages, set())

    def test_missing_invalid_and_denied_optional_context_preserve_success_results(self):
        for change in ("missing", "invalid", "denied"):
            with self.subTest(change=change):
                self.contract["denied_operations"] = ["create", "delete"]
                self.new_runner()
                if change == "missing":
                    self.source.unlink()
                elif change == "invalid":
                    self.source.write_bytes(b"def broken(\n")
                else:
                    self.contract["denied_operations"].append("read")
                self.assertEqual(self.invoke(), self.observation)
                self.assertTrue(self.runner.cleanup_pass)
                self.assertEqual(self.runner._emitted_source_postimages, set())

    def test_optional_capture_and_projection_exceptions_do_not_change_raw_results(self):
        with patch.object(verifier_parent.postimage_context, "project", side_effect=RuntimeError("optional")):
            self.assertEqual(self.invoke(), self.observation)
        self.assertEqual(self.runner._emitted_source_postimages, set())
        self.assertIn("source_postimages", self.invoke(call_id="next"))
        with patch.object(verifier_parent.postimage_context, "capture_preimages", side_effect=OSError("optional")):
            self.runner = ParentVerifier(self.workspace, self.contract, self.router, None)
        self.assertIsNone(self.runner._source_preimages)
        self.assertEqual(self.invoke(), self.observation)

    def test_supervisor_failure_or_invalid_observation_is_not_context_or_cleanup_proof(self):
        with self.assertRaisesRegex(ValueError, "VERIFIER_SUPERVISOR_FAILED"):
            self.invoke(supervisor_exit=1)
        self.assertFalse(self.runner.cleanup_pass)
        self.assertEqual(self.runner._emitted_source_postimages, set())
        self.new_runner()
        with self.assertRaisesRegex(ValueError, "VERIFIER_OBSERVATION_INVALID"):
            self.invoke(dict(self.observation, exit_code=True))
        self.assertFalse(self.runner.cleanup_pass)
        self.assertEqual(self.runner._emitted_source_postimages, set())


class ParentVerifierEvidenceTests(unittest.TestCase):
    """Wire proof is independent of advisory source context and covers every target."""

    new_runner = ParentVerifierPostimageTests.new_runner
    invoke = ParentVerifierPostimageTests.invoke

    def setUp(self):
        ParentVerifierPostimageTests.setUp(self)
        self.contract["declared_targets"] = ["work.py"]

    def test_exact_wire_contract_all_targets_and_no_dedup_or_raw_rewrite(self):
        other = self.workspace / "config.bin"
        other.write_bytes(b"\x00\xff\n")
        other.chmod(0o640)
        for key in ("read_scopes", "write_scopes", "declared_targets"):
            self.contract[key].append("config.bin")
        self.contract["verifier_commands"][0]["python_import_roots"] = [str(self.workspace)]
        grant = copy.deepcopy(self.contract["verifier_commands"][0])
        expected = {name: verifier_parent.file_change_evidence._postimage(self.workspace, name)
                    for name in self.contract["declared_targets"]}
        for call_id in ("runtime.tool.command_run:call_first:0", "runtime.tool.command_run:call_second:0"):
            result = self.invoke(call_id=call_id)
            self.assertEqual({key: result[key] for key in self.observation}, self.observation)
            self.assertEqual(result["verification_evidence"], {
                "schema_version": "nokiy_focused_verifier_evidence_v1",
                "authorization_semantic_sha256": self.runner.binding,
                "verifier_index": 0, "verifier_sha256": caller._canonical_sha256(grant),
                "call_id": call_id, "source_postimages": expected,
            })
        self.assertNotIn("verification_evidence", self.observation)
        self.assertEqual(self.contract["verifier_commands"][0], grant)
        self.assertTrue(self.runner.cleanup_pass)

    def test_empty_targets_emit_empty_map_without_reading_or_needing_advisory_context(self):
        self.contract.update(write_scopes=[], declared_targets=[])
        with patch.object(verifier_parent.file_change_evidence, "_postimage",
                          side_effect=AssertionError("no file reads")):
            result = self.invoke(call_id="empty")
        self.assertEqual(result["verification_evidence"]["source_postimages"], {})

    def test_failure_unknown_and_unproven_cleanup_never_emit_evidence(self):
        for change in ({"success": False, "exit_code": 1}, {"outcome": "unknown"},
                       {"process_reaped": False}, {"process_group_empty": False}):
            with self.subTest(change=change):
                result = self.invoke(dict(self.observation, **change), call_id=str(change))
                self.assertNotIn("verification_evidence", result)
                self.assertEqual(result, dict(self.observation, **change))

    def test_bytes_mode_identity_and_aba_drift_omit_proof_without_changing_success(self):
        def replace_same_bytes():
            replacement = self.workspace / "replacement.py"
            replacement.write_bytes(self.source.read_bytes())
            replacement.replace(self.source)
        def rewrite_same_bytes():
            self.source.write_bytes(self.source.read_bytes())
        actions = (lambda: self.source.write_bytes(b"different\n"),
                   lambda: self.source.chmod(0o600), replace_same_bytes, rewrite_same_bytes,
                   lambda: self.source.unlink())
        for action in actions:
            with self.subTest(action=action):
                self.new_runner()
                self.source.chmod(0o644)
                result = self.invoke(during_execution=action)
                self.assertNotIn("verification_evidence", result)
                self.assertEqual({key: result[key] for key in self.observation}, self.observation)
                self.assertTrue(self.runner.cleanup_pass)

    def test_missing_final_or_ancestor_symlink_is_never_source_proof(self):
        outside = self.workspace / "outside.py"
        outside.write_bytes(self.after.encode())
        self.source.unlink()
        self.source.symlink_to(outside)
        result = self.invoke()
        self.assertNotIn("verification_evidence", result)
        self.source.unlink()
        self.new_runner()
        directory = self.workspace / "real"
        directory.mkdir()
        (directory / "work.py").write_bytes(self.after.encode())
        (self.workspace / "alias").symlink_to(directory, target_is_directory=True)
        for key in ("read_scopes", "write_scopes", "declared_targets"):
            self.contract[key] = ["alias/work.py"]
        self.assertNotIn("verification_evidence", self.invoke())
        for key in ("read_scopes", "write_scopes", "declared_targets"):
            self.contract[key] = ["missing.py"]
        self.assertNotIn("verification_evidence", self.invoke())

    def test_capture_budgets_unsupported_scope_and_exceptions_are_optional(self):
        for changes in ({"declared_targets": []}, {"write_scopes": ["work*.py"]},
                        {"read_scopes": []}, {"denied_operations": ["read"]},
                        {"declared_targets": [f"{i}.py" for i in range(129)]}):
            with self.subTest(changes=changes):
                contract = copy.deepcopy(self.contract)
                self.contract.update(changes)
                self.assertNotIn("verification_evidence", self.invoke())
                self.contract.clear()
                self.contract.update(contract)
        with patch.object(verifier_parent.file_change_evidence, "MAX_FILE_BYTES", 1):
            self.assertNotIn("verification_evidence", self.invoke())
        with patch.object(verifier_parent.file_change_evidence, "_postimage", side_effect=OSError("optional")):
            self.assertNotIn("verification_evidence", self.invoke())
        self.assertIn("verification_evidence", self.invoke(call_id="fresh"))

    def test_grant_binding_and_target_drift_during_execution_omit_proof(self):
        for change in (lambda: self.contract["verifier_commands"][0].update(timeout_seconds=8),
                       lambda: self.contract.update(authorization_semantic_sha256="b" * 64),
                       lambda: self.contract.update(declared_targets=[])):
            with self.subTest(change=change):
                contract = copy.deepcopy(self.contract)
                self.assertNotIn("verification_evidence", self.invoke(during_execution=change))
                self.contract.clear()
                self.contract.update(contract)

    def test_failed_finalization_cannot_publish_proof(self):
        with self.assertRaisesRegex(OSError, "stream close failed"):
            self.invoke(close_error=True)
        self.assertIn("verification_evidence", self.invoke(call_id="later"))

    def test_cancellation_even_with_a_mocked_success_never_emits_proof(self):
        cancelled = threading.Event()
        with patch.object(verifier_parent.threading, "Event", return_value=cancelled):
            result = self.invoke(during_execution=cancelled.set)
        self.assertNotIn("verification_evidence", result)
        self.assertEqual({key: result[key] for key in self.observation}, self.observation)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("NOKIY_VERIFIER_TEST_ROUTER"),
                     "requires exact candidate router and real macOS Seatbelt")
class VerifierParentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("NOKIY_TEST_ROOT"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.artifacts = self.root / "artifacts"
        self.scratch = self.artifacts / "verifier"
        self.workspace.mkdir()
        self.scratch.mkdir(parents=True)
        self.script = self.workspace / "verify.py"
        self.solution = self.workspace / "solution.txt"
        self.solution.write_text("broken")
        self.python = Path(sys.executable).resolve()
        router = Path(os.environ["NOKIY_VERIFIER_TEST_ROUTER"]).resolve()
        self.router = caller.FileIdentity(router, caller._file_sha256(router))

    def runner(self, body, timeout=5, *, python_import_roots=None, read_scopes=()):
        self.script.write_text(body)
        action = {
            "mission":{"mission_id":"parent-verifier-tests","task_id":"exact-fixture",
                       "mode":"DELIVERY","objective":"Exercise scoped public verifier",
                       "current_predicate":"Parent channel acceptance"},
            "context_summary":"Disposable fixture; no provider send or production mutation.",
            "operations":["read","command","modify"],
            "read_scopes":["verify.py","solution.txt", *read_scopes],
            "write_scopes":["solution.txt"],"target_paths":["solution.txt"],
            "command_templates":[], "source_read":True,
            "forbidden_effects":["network","delete"],
            "verifier_commands":[{"argv":[str(self.python),str(self.script)],
                "executable_sha256":caller._file_sha256(self.python),
                "pinned_files":[{"path":str(self.script),"sha256":caller._file_sha256(self.script)}],
                "timeout_seconds":timeout,"scratch_root":str(self.scratch),"network":False}],
        }
        if python_import_roots is not None:
            action["verifier_commands"][0]["python_import_roots"] = list(python_import_roots)
        _, contract = local_context.compile_context(self.workspace, action, artifact_root=self.artifacts)
        return ParentVerifier(self.workspace, contract, self.router, time.monotonic()+20)

    def assert_wire_bounds(self, result, runner, call_id):
        inner = (json.dumps(result, ensure_ascii=True, allow_nan=False) + "\n").encode()
        self.assertLessEqual(len(inner), 262144)
        reply = {"version": 1, "binding": runner.binding, "call_id": call_id,
                 "verifier_index": 0, "result": result}
        outer = (json.dumps(reply, ensure_ascii=True, allow_nan=False,
                            sort_keys=True, separators=(",", ":")) + "\n").encode()
        self.assertLessEqual(len(outer), 262144)
        self.assertEqual(json.loads(outer)["result"], result)

    def test_real_noisy_finite_success_and_failure_retain_actual_exit_and_tail(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                runner = self.runner(
                    "import sys,time\n"
                    "for stream,label in ((sys.stdout.buffer,b'stdout'),(sys.stderr.buffer,b'stderr')):\n"
                    " stream.write(label+b'-head\\n'+b'x'*180224+b'\\n'+label+b'-tail\\n')\n"
                    " stream.flush()\n"
                    f"time.sleep(.1)\nsys.exit({exit_code})\n")
                call_id = f"noisy-{exit_code}"
                result = runner(0, call_id, threading.Event())
                self.assertEqual(result["exit_code"], exit_code, result)
                self.assertEqual(result["success"], exit_code == 0, result)
                self.assertEqual(result["outcome"], "known", result)
                self.assertTrue(result["process_reaped"] and result["process_group_empty"], result)
                self.assertTrue(runner.cleanup_pass, result)
                for stream in ("stdout", "stderr"):
                    self.assertTrue(result[stream].startswith(stream + "-head\n"))
                    self.assertTrue(result[stream].endswith("\n" + stream + "-tail\n"))
                    self.assertIn("diagnostic bytes truncated", result[stream])
                self.assert_wire_bounds(result, runner, call_id)

    def test_real_difficult_noisy_bytes_fit_both_json_envelopes(self):
        cases = (("controls", b"\x00"),
                 ("unicode", "\U0001f600\u2028é\"\\\n".encode()),
                 ("invalid", b"\xff\xfe\xf0\x80"))
        for name, unit in cases:
            with self.subTest(name=name):
                runner = self.runner(
                    "import sys\n"
                    f"noise={unit!r}*{180224 // len(unit) + 1}\n"
                    "for stream in (sys.stdout.buffer,sys.stderr.buffer):\n"
                    " stream.write(b'head\\n'+noise+b'\\ntail\\n')\n stream.flush()\n")
                call_id = name + ":" + "x" * (255 - len(name))
                result = runner(0, call_id, threading.Event())
                self.assertTrue(result["success"], result)
                self.assertEqual(result["exit_code"], 0, result)
                self.assertEqual(result["outcome"], "known", result)
                self.assertTrue(result["process_reaped"] and result["process_group_empty"], result)
                self.assertTrue(runner.cleanup_pass, result)
                for stream in ("stdout", "stderr"):
                    self.assertTrue(result[stream].startswith("head\n"))
                    self.assertTrue(result[stream].endswith("\ntail\n"))
                    self.assertIn(unit.decode("utf-8", "replace"), result[stream])
                    self.assertIn("diagnostic bytes truncated", result[stream])
                self.assert_wire_bounds(result, runner, call_id)

    def test_real_noisy_timeout_remains_unknown_with_clean_supervision(self):
        runner = self.runner(
            "import sys,time\n"
            "sys.stderr.buffer.write(b'x'*180224+b'\\ntimeout-tail\\n')\n"
            "sys.stderr.buffer.flush()\ntime.sleep(10)\n", timeout=2)
        result = runner(0, "noisy-timeout", threading.Event())
        self.assertFalse(result["success"], result)
        self.assertEqual(result["outcome"], "unknown", result)
        self.assertNotEqual(result["exit_code"], 0, result)
        self.assertTrue(result["stderr"].endswith("\ntimeout-tail\n"))
        self.assertIn("diagnostic bytes truncated", result["stderr"])
        self.assertTrue(result["process_reaped"] and result["process_group_empty"], result)
        self.assertTrue(runner.cleanup_pass, result)

    def test_real_import_roots_enable_admitted_source_but_not_sibling_or_outside_reads(self):
        source_root = self.workspace / "src"
        source_root.mkdir()
        source = source_root / "admitted_source.py"
        source.write_text("VALUE = 42\n")
        sibling = source_root / "unread.txt"
        outside = self.root / "outside-import-root.txt"
        sibling.write_text("unadmitted sibling")
        outside.write_text("outside workspace")
        body = (
            "import admitted_source\nfrom pathlib import Path\n"
            "assert admitted_source.VALUE == 42\n"
            f"for denied in ({str(sibling)!r}, {str(outside)!r}):\n"
            " try: Path(denied).read_text()\n"
            " except PermissionError: pass\n"
            " else: raise AssertionError('import root expanded read scope')\n"
            "print('admitted-source-confined', flush=True)\n")
        baseline = self.runner(body, read_scopes=("src/admitted_source.py",))
        first = baseline(0, "without-roots", threading.Event())
        self.assertFalse(first["success"], first)
        self.assertIn("ModuleNotFoundError", first["stderr"])
        self.assertNotIn("python_import_roots", baseline.contract["verifier_commands"][0])
        self.assertTrue(first["process_reaped"] and first["process_group_empty"], first)
        self.assertTrue(baseline.cleanup_pass)
        runner = self.runner(body, python_import_roots=[str(source_root)],
                             read_scopes=("src/admitted_source.py",))
        self.assertNotEqual(runner.binding, baseline.binding)
        result = runner(0, "with-roots", threading.Event())
        self.assertTrue(result["success"], result)
        self.assertIn("admitted-source-confined", result["stdout"])
        self.assertTrue(result["process_reaped"] and result["process_group_empty"], result)
        self.assertTrue(runner.cleanup_pass)
        self.assertFalse((source_root / "__pycache__").exists())
        self.assertFalse((self.workspace / ".tura").exists())
        self.assertFalse((self.scratch / ".tura").exists())

    def test_real_source_import_keeps_setsid_descendant_cleanup(self):
        source_root = self.workspace / "src"
        source_root.mkdir()
        (source_root / "admitted_source.py").write_text("VALUE = 42\n")
        runner = self.runner(
            "import admitted_source\nimport os,time\nfrom pathlib import Path\n"
            "assert admitted_source.VALUE == 42\n"
            "pid=os.fork()\n"
            "if pid==0:\n os.setsid()\n time.sleep(4)\n os._exit(0)\n"
            f"Path({str(self.scratch / 'import-child.pid')!r}).write_text(str(pid))\n"
            "time.sleep(.08)\n", python_import_roots=[str(source_root)],
            read_scopes=("src/admitted_source.py",))
        result = runner(0, "import-descendant", threading.Event())
        self.assertFalse(result["success"], result)
        self.assertEqual(result["outcome"], "unknown", result)
        self.assertTrue(runner.cleanup_pass, result)
        pid = int((self.scratch / "import-child.pid").read_text())
        info = DarwinProcesses().info(pid)
        self.assertTrue(info is None or info.status == 5)
        self.assertFalse((source_root / "__pycache__").exists())

    def test_real_failure_edit_success_and_no_second_receipt_tree(self):
        runner = self.runner("from pathlib import Path\nimport sys\n"
                             "print('public-test', flush=True)\n"
                             "sys.exit(0 if Path('solution.txt').read_text() == 'fixed' else 1)\n")
        first = runner(0, "first", threading.Event())
        self.assertEqual(first["exit_code"], 1, first)
        self.assertFalse(first["success"], first)
        self.assertEqual(first["outcome"], "known", first)
        self.assertTrue(runner.cleanup_pass)
        self.solution.write_text("fixed")
        second = runner(0, "second", threading.Event())
        self.assertTrue(second["success"], second)
        self.assertIn("public-test", second["stdout"])
        self.assertTrue(runner.cleanup_pass)
        self.assertFalse((self.workspace / ".tura").exists())
        self.assertFalse((self.scratch / ".tura").exists())

    def test_real_scope_rejects_outside_read_write_and_network(self):
        secret = self.root / "outside.txt"
        secret.write_text("outside")
        runner = self.runner(
            "from pathlib import Path\nimport socket\n"
            f"scratch=Path({str(self.scratch)!r})\n"
            "(scratch/'allowed').write_text('yes')\n"
            f"for action in [lambda:Path({str(secret)!r}).read_text(), "
            "lambda:Path('solution.txt').write_text('bad'), "
            "lambda:socket.socket().connect(('127.0.0.1',9))]:\n"
            " try: action()\n except PermissionError: pass\n else: raise AssertionError('escaped scope')\n"
            "print('confined')\n")
        result = runner(0, "scope", threading.Event())
        self.assertTrue(result["success"], result)
        self.assertEqual(self.solution.read_text(), "broken")
        self.assertEqual((self.scratch / "allowed").read_text(), "yes")

    def test_real_scope_cleans_setsid_descendant(self):
        runner = self.runner(
            "import os,sys,time\nfrom pathlib import Path\n"
            "sys.stdout.buffer.write(b'x'*180224+b'\\ndescendant-tail\\n')\n"
            "sys.stdout.buffer.flush()\n"
            "pid=os.fork()\n"
            "if pid==0:\n os.setsid()\n time.sleep(4)\n os._exit(0)\n"
            f"Path({str(self.scratch/'child.pid')!r}).write_text(str(pid))\n"
            "time.sleep(.08)\n")
        result = runner(0, "descendant", threading.Event())
        self.assertFalse(result["success"], result)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["outcome"], "unknown", result)
        self.assertTrue(result["stdout"].endswith("\ndescendant-tail\n"))
        self.assertIn("diagnostic bytes truncated", result["stdout"])
        self.assertTrue(runner.cleanup_pass, result)
        pid = int((self.scratch / "child.pid").read_text())
        info = DarwinProcesses().info(pid)
        self.assertTrue(info is None or info.status == 5)

    def test_real_cancellation_reaps_before_return(self):
        marker = self.scratch / "ready"
        runner = self.runner("import sys,time\nfrom pathlib import Path\n"
                             "sys.stderr.buffer.write(b'x'*180224+b'\\ncancel-tail\\n')\n"
                             "sys.stderr.buffer.flush()\n"
                             f"Path({str(marker)!r}).touch()\ntime.sleep(5)\n")
        cancelled = threading.Event()
        def cancel_started():
            deadline = time.monotonic()+8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            cancelled.set()
        timer = threading.Thread(target=cancel_started)
        timer.start()
        self.addCleanup(timer.join)
        result = runner(0, "cancelled", cancelled)
        self.assertTrue(marker.exists())
        self.assertFalse(result["success"], result)
        self.assertEqual(result["outcome"], "unknown", result)
        self.assertTrue(result["stderr"].endswith("\ncancel-tail\n"))
        self.assertIn("diagnostic bytes truncated", result["stderr"])
        self.assertTrue(result["process_reaped"] and result["process_group_empty"], result)
        self.assertTrue(runner.cleanup_pass, result)

    def test_identical_fresh_sandbox_sibling_survives(self):
        runner = self.runner("print('ok')\n")
        plan = subprocess.run([str(self.router.path),"focused-verifier-plan"],
            input=json.dumps({"workspace":str(self.workspace),"contract":runner.contract,
                              "binding":runner.binding,"verifier_index":0}).encode(),
            capture_output=True, check=True)
        profile, python, _ = supervisor_policy(json.loads(plan.stdout)["profile"])
        sibling = subprocess.Popen(["/usr/bin/sandbox-exec","-p",profile,str(python),
                                    "-B","-c","import time;time.sleep(5)"],
                                   cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={"HOME":os.environ["HOME"],"PYTHONDONTWRITEBYTECODE":"1"})
        try:
            time.sleep(.1)
            self.assertIsNone(sibling.poll())
            result = runner(0, "sibling", threading.Event())
            self.assertTrue(result["success"], result)
            self.assertIsNone(sibling.poll())
        finally:
            sibling.send_signal(signal.SIGKILL)
            sibling.communicate(timeout=5)

    def test_pin_drift_rejects_before_test_start(self):
        runner = self.runner("print('must not run')\n")
        self.script.write_text("print('changed')\n")
        with self.assertRaises(subprocess.CalledProcessError):
            runner(0, "drift", threading.Event())
        self.assertEqual(runner.calls, 0)
        self.assertTrue(runner.cleanup_pass)
