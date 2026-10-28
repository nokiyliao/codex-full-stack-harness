# SPDX-License-Identifier: MIT
"""Real scoped verifier acceptance, opt-in against an exact candidate router."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from test_postimage_context import dcf_generation

from codex_collaboration_harness import graph_entry, verifier_parent
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
                self.assertEqual(kwargs["env"], {
                    **{key: value for key, value in expected_env.items()
                       if key not in {"TMPDIR", "PYTHONPATH"}}, "PATH": "/usr/bin:/bin"})
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
        self.last_spawn, self.last_process = spawn, process
        return result

    # V318: a granted cwd moves where the supervisor starts and what it is asked to run in.
    def test_granted_cwd_starts_the_supervisor_there(self):
        grant = self.contract["verifier_commands"][0]
        for cwd in (None, str(self.workspace / "elsewhere")):
            with self.subTest(cwd=cwd):
                grant.pop("cwd", None)
                if cwd is not None:
                    grant["cwd"] = cwd
                self.new_runner()
                self.assertTrue(self.invoke()["success"])
                start = cwd or str(self.workspace)
                self.assertEqual(self.last_spawn.call_args.kwargs["cwd"], start)
                request = json.loads(self.last_process.stdin.write.call_args.args[0])
                self.assertEqual(request, {"argv": grant["argv"], "cwd": start})

    def test_cwd_plan_mismatch_rejects_before_policy_or_spawn(self):
        field, cwd = "cwd", str(self.workspace / "elsewhere")
        for bound, echoed in (({}, {field: cwd}), ({field: cwd}, {}), ({field: cwd}, {field: None}),
                              ({field: cwd}, {field: str(self.workspace)})):
            with self.subTest(bound=bound, echoed=echoed):
                grant = self.contract["verifier_commands"][0]
                grant.pop(field, None)
                grant.update(bound)
                self.new_runner()
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

    def node_command(self, suffix=".js", *, bin_name="bin"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        executable = root / bin_name / "node"
        executable.parent.mkdir()
        # The transport is mocked; this executable is used only for PATH lookup.
        executable.write_bytes(Path(__file__).read_bytes())
        executable.chmod(0o700)
        entry = root / ("npm-cli" + suffix)
        entry.write_text("#!/usr/bin/env node\n")
        self.contract["verifier_commands"][0].update(
            argv=[str(executable), str(entry), "test"],
            executable_sha256=caller._file_sha256(executable),
            pinned_files=[{"path": str(entry), "sha256": caller._file_sha256(entry),
                           "scope": "verifier_only"}])
        return executable, entry

    def test_node_external_program_resolves_pinned_child_without_ambient_inheritance(self):
        ambient = {"HOME": "/actual-home", "CODEX_HOME": "/actual-codex-home",
                   "PATH": "/ambient-bin", "NODE_OPTIONS": "--require=/ambient.js",
                   "NODE_PATH": "/ambient-modules", "NPM_CONFIG_USERCONFIG": "/ambient-npmrc",
                   "npm_config_prefix": "/ambient-prefix", "npm_config_node_options": "--inspect",
                   "PYTHONPATH": "/ambient-python", "TMPDIR": "/ambient-tmp"}
        for suffix in (".js", ".cjs", ".mjs"):
            with self.subTest(suffix=suffix):
                executable, _ = self.node_command(suffix)
                expected = {"HOME": ambient["HOME"], "CODEX_HOME": ambient["CODEX_HOME"],
                            "PATH": os.pathsep.join((str(executable.parent), "/usr/bin", "/bin")),
                            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                            "LANG": "C.UTF-8", "TMPDIR": "/scratch"}
                with patch.dict(os.environ, ambient, clear=True):
                    self.assertTrue(self.invoke(expected_env=expected)["success"])
                child_env = self.last_spawn.call_args.kwargs["env"]
                self.assertEqual(shutil.which("node", path=child_env["PATH"]), str(executable))
                request = json.loads(self.last_process.stdin.write.call_args.args[0])
                self.assertEqual(request["argv"], self.contract["verifier_commands"][0]["argv"])
                self.assertTrue(self.runner.cleanup_pass)

    def test_node_colon_directory_rejects_before_policy_or_spawn(self):
        self.node_command(bin_name="bin:extra")
        grant = self.contract["verifier_commands"][0]
        grant_sha256 = caller._canonical_sha256(grant)
        plan = dict(grant, binding=self.runner.binding, verifier_index=0, profile="unchanged-policy")
        with patch.object(verifier_parent.subprocess, "run",
                          return_value=SimpleNamespace(stdout=json.dumps(plan).encode())) as planned, \
                patch.object(verifier_parent.subprocess, "Popen") as spawn, \
                patch.object(verifier_parent, "supervisor_policy") as policy:
            result = self.runner.handle_request(0, "node-path", threading.Event())
        planned.assert_called_once()
        spawn.assert_not_called()
        policy.assert_not_called()
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], -1)
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["outcome"], "known")
        self.assertTrue(result["process_reaped"])
        self.assertTrue(result["process_group_empty"])
        self.assertEqual(json.loads(result["stderr"]), {
            "error_type": "VERIFIER_PREPARATION_REJECTED", "message": "VERIFIER_NODE_PATH_INVALID",
            "authorization_semantic_sha256": self.runner.binding, "verifier_index": 0,
            "verifier_sha256": grant_sha256, "call_id": "node-path", "process_started": False,
            "retry_safe": False, "auto_retry_allowed": False, "diagnostics": {}})
        self.assertEqual(caller._canonical_sha256(grant), grant_sha256)
        self.assertEqual(self.runner.calls, 0)
        self.assertTrue(self.runner.cleanup_pass)

    def test_node_path_requires_direct_bound_external_javascript_entry(self):
        executable, entry = self.node_command()
        grant = self.contract["verifier_commands"][0]
        pin, = grant["pinned_files"]
        argv = list(grant["argv"])
        internal = str(self.workspace / "verify.js")
        cases = [
            ([str(executable.with_name("node-wrapper")), *argv[1:]], [pin]),
            (argv, []),
            (argv, [{**pin, "path": str(entry.with_name("other.js"))}]),
            (argv, [{"path": pin["path"], "sha256": pin["sha256"]}]),
            ([str(executable), str(entry.with_suffix(".py")), "test"],
             [{**pin, "path": str(entry.with_suffix(".py"))}]),
            ([str(executable), "--test", str(entry)], [pin]),
            ([str(executable), internal, "test"], [{**pin, "path": internal}]),
        ]
        expected = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1", "LANG": "C.UTF-8", "TMPDIR": "/scratch"}
        for command, pins in cases:
            with self.subTest(argv=command, pins=pins):
                grant.update(argv=command, pinned_files=pins)
                with patch.dict(os.environ, {"PATH": "/ambient-bin", "NODE_OPTIONS": "--inspect",
                                             "npm_config_prefix": "/ambient-prefix"}, clear=True):
                    self.assertTrue(self.invoke(expected_env=expected)["success"])

    def test_node_plan_cannot_redirect_bound_executable_or_entry(self):
        executable, entry = self.node_command()
        grant = self.contract["verifier_commands"][0]
        for position, replacement in ((0, str(executable.with_name("other-node"))),
                                      (1, str(entry.with_name("other.js")))):
            with self.subTest(position=position):
                plan = dict(grant, argv=list(grant["argv"]), binding=self.runner.binding,
                            verifier_index=0, profile="unchanged-policy")
                plan["argv"][position] = replacement
                with patch.object(verifier_parent.subprocess, "run",
                                  return_value=SimpleNamespace(stdout=json.dumps(plan).encode())), \
                        patch.object(verifier_parent.subprocess, "Popen") as spawn, \
                        patch.object(verifier_parent, "supervisor_policy") as policy:
                    with self.assertRaisesRegex(ValueError, "VERIFIER_PLAN_IDENTITY_MISMATCH"):
                        self.runner(0, "node-mismatch", threading.Event())
                spawn.assert_not_called()
                policy.assert_not_called()
                self.assertEqual(self.runner.calls, 0)
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

    def test_omitted_proof_logs_reason_without_changing_result_schema(self):
        with patch.object(verifier_parent, "_capture_verification_sources", return_value=None), \
                self.assertLogs(verifier_parent.__name__, level="WARNING") as logs:
            result = self.invoke()
        self.assertNotIn("verification_evidence", result)
        self.assertNotIn("verification_diagnostic", result)
        self.assertIn('"reason": "PREIMAGE_UNAVAILABLE"', logs.output[0])
        self.assertEqual({key: result[key] for key in self.observation}, self.observation)

    def test_changed_source_logs_identity_failure_not_success(self):
        with self.assertLogs(verifier_parent.__name__, level="WARNING") as logs:
            result = self.invoke(during_execution=lambda: self.source.write_text("changed\n"))
        self.assertNotIn("verification_evidence", result)
        self.assertIn('"reason": "SOURCE_IDENTITY_CHANGED"', logs.output[0])
        self.assertIn('"kind": "SOURCE_POSTIMAGE_CHANGED"', logs.output[0])

    def test_sibling_creation_reports_absence_fence_without_granting_success(self):
        target = self.admit_absent_target()
        with self.assertLogs(verifier_parent.__name__, level="WARNING") as logs:
            result = self.invoke(during_execution=lambda:
                                 (self.workspace / "other_branch.py").write_text("sibling\n"))
        self.assertFalse(target.exists())
        self.assertNotIn("verification_evidence", result)
        self.assertIn('"kind": "ABSENCE_PARENT_METADATA_CHANGED"', logs.output[0])
        self.assertIn('"path": "optional.py"', logs.output[0])
        self.assertEqual({key: result[key] for key in self.observation}, self.observation)

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

    def admit_absent_target(self, name="optional.py"):
        for key in ("read_scopes", "write_scopes", "declared_targets"):
            self.contract[key].append(name)
        self.contract["dcf_generation"]["source_snapshot"][name] = None
        self.contract["allowed_operations"].append("create")
        self.contract["denied_operations"].remove("create")
        return self.workspace / name

    def test_admitted_uncreated_target_emits_explicit_null_without_changing_success(self):
        target = self.admit_absent_target()
        original = copy.deepcopy(self.contract)
        result = self.invoke()
        self.assertEqual({key: result[key] for key in self.observation}, self.observation)
        self.assertEqual(set(result["verification_evidence"]["source_postimages"]),
                         {"work.py", target.name})
        self.assertIsNone(result["verification_evidence"]["source_postimages"][target.name])
        self.assertFalse(target.exists())
        self.assertEqual(self.contract, original)

    def test_absence_needs_explicit_initial_null_and_admitted_creation(self):
        self.admit_absent_target()
        original = copy.deepcopy(self.contract)
        mutations = (
            lambda: self.contract["dcf_generation"]["source_snapshot"].pop("optional.py"),
            lambda: self.contract["dcf_generation"]["source_snapshot"].update({"optional.py": {}}),
            lambda: self.contract["allowed_operations"].remove("create"),
            lambda: self.contract["denied_operations"].append("create"),
        )
        for mutation in mutations:
            self.contract.clear()
            self.contract.update(copy.deepcopy(original))
            mutation()
            with self.subTest(mutation=mutation):
                self.assertNotIn("verification_evidence", self.invoke())

    def test_absent_target_appearance_symlink_and_create_remove_aba_omit_proof(self):
        target = self.admit_absent_target()
        def transient_creation():
            target.write_text("temporary")
            target.unlink()
        for mutation in (lambda: target.write_text("created"),
                         lambda: target.symlink_to(self.source), transient_creation):
            with self.subTest(mutation=mutation):
                result = self.invoke(during_execution=mutation)
                self.assertNotIn("verification_evidence", result)
                self.assertEqual({key: result[key] for key in self.observation}, self.observation)
                target.unlink(missing_ok=True)

    def test_absence_cannot_prove_a_missing_parent_directory(self):
        self.admit_absent_target("missing/optional.py")
        self.assertNotIn("verification_evidence", self.invoke())

    def test_explicit_readonly_subjects_are_copied_and_do_not_grant_writes(self):
        self.contract.update(write_scopes=[], declared_targets=[], allowed_operations=["read", "command"],
                             denied_operations=["create", "modify", "delete", "network"])
        subjects = ["work.py"]
        self.runner = ParentVerifier(self.workspace, self.contract, self.router, None,
                                     verification_source_targets=subjects)
        subjects.clear()
        original = copy.deepcopy(self.contract)
        result = self.invoke()
        self.assertEqual(set(result["verification_evidence"]["source_postimages"]), {"work.py"})
        self.assertEqual(self.contract, original)
        self.assertEqual(self.runner._verification_source_targets, ["work.py"])
        self.assertNotIn("verification_evidence", self.invoke(
            during_execution=lambda: self.source.write_text("raced")))

    def test_explicit_subjects_cannot_expand_reads_or_follow_symlinks(self):
        self.contract.update(write_scopes=[], declared_targets=[], allowed_operations=["read", "command"])
        for subjects in (["outside.py"], ["work.py", "work.py"], ["../work.py"], ["work*.py"],
                         [f"{i}.py" for i in range(129)]):
            with self.subTest(subjects=subjects):
                self.runner = ParentVerifier(self.workspace, self.contract, self.router, None,
                                             verification_source_targets=subjects)
                self.assertNotIn("verification_evidence", self.invoke())
        outside = self.workspace / "outside.py"
        outside.write_text("private")
        self.source.unlink()
        self.source.symlink_to(outside)
        self.runner = ParentVerifier(self.workspace, self.contract, self.router, None,
                                     verification_source_targets=["work.py"])
        self.assertNotIn("verification_evidence", self.invoke())

    def test_explicit_readonly_absence_needs_no_create_grant(self):
        self.contract.update(write_scopes=[], declared_targets=[], allowed_operations=["read", "command"],
                             denied_operations=["create", "modify", "delete", "network"])
        self.contract["read_scopes"].append("optional.py")
        self.runner = ParentVerifier(self.workspace, self.contract, self.router, None,
                                     verification_source_targets=["work.py", "optional.py"])
        original = copy.deepcopy(self.contract)
        result = self.invoke()
        self.assertIsNone(result["verification_evidence"]["source_postimages"]["optional.py"])
        self.assertEqual(self.contract, original)
        target = self.workspace / "optional.py"
        def transient_creation():
            target.write_text("temporary")
            target.unlink()
        for mutation in (lambda: target.write_text("created"),
                         lambda: target.symlink_to(self.source), transient_creation):
            with self.subTest(mutation=mutation):
                self.assertNotIn("verification_evidence", self.invoke(during_execution=mutation))
                target.unlink(missing_ok=True)

    def test_explicit_directory_subject_can_prove_absence_with_bound_parent(self):
        directory = self.workspace / "sources"
        directory.mkdir()
        self.contract.update(write_scopes=[], declared_targets=[], allowed_operations=["read", "command"],
                             denied_operations=["create", "modify", "delete", "network"],
                             read_scopes=["sources/**"], read_commands={"roots": ["sources"],
                             "rg": {"path": "/usr/bin/rg", "sha256": "a" * 64},
                             "cat": {"path": "/bin/cat", "sha256": "b" * 64}})
        self.contract["dcf_generation"]["directory_snapshot"] = (
            verifier_parent.local_context._directories(self.workspace, ["sources"]))
        self.runner = ParentVerifier(self.workspace, self.contract, self.router, None,
                                     verification_source_targets=["sources/optional.py"])
        original = copy.deepcopy(self.contract)
        self.assertEqual(self.invoke()["verification_evidence"]["source_postimages"],
                         {"sources/optional.py": None})
        self.assertEqual(self.contract, original)
        target = directory / "optional.py"
        self.assertNotIn("verification_evidence", self.invoke(
            during_execution=lambda: target.symlink_to(self.source)))
        target.unlink()
        for subject in ("outside/optional.py", "sources/missing/optional.py"):
            self.runner = ParentVerifier(self.workspace, self.contract, self.router, None,
                                         verification_source_targets=[subject])
            self.assertNotIn("verification_evidence", self.invoke())

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


class VerifierOutputTests(unittest.TestCase):
    """Real byte pipes/files; native isolation alone is replaced by a test stub."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.run = self.root / "request"
        self.scratch = self.root / "scratch"
        for directory in (self.workspace, self.run, self.scratch):
            directory.mkdir(mode=0o700)

    @staticmethod
    def supervisor_helper(suffix=""):
        return ("from codex_collaboration_harness import graph_process as g\n"
                "class Processes:\n"
                " def verify_isolation(self, parent): pass\n"
                " def members(self): return []\n"
                "g.DarwinProcesses = Processes\n" + suffix +
                "raise SystemExit(g.focused_verifier_main())\n")

    def invoke(self, body, *, root=None, timeout=7, call_id="output", helper_suffix=""):
        from codex_collaboration_harness import graph_process
        grant = {"argv": [sys.executable, "-B", "-c", body],
                 "scratch_root": str(self.scratch), "timeout_seconds": timeout,
                 "python_import_roots": [str(Path(graph_process.__file__).resolve().parent.parent)]}
        contract = {"authorization_semantic_sha256": "a" * 64, "verifier_commands": [grant]}
        router = SimpleNamespace(path=Path("/router"), verify=Mock())
        runner = ParentVerifier(self.workspace, contract, router, None,
            diagnostic_root=self.run if root is None else root, request_id="request",
            request_sha256="b" * 64)
        plan = dict(grant, binding=runner.binding, verifier_index=0, profile="unchanged-policy")
        real_popen = subprocess.Popen
        helper = self.supervisor_helper(helper_suffix)
        def spawn(command, **kwargs):
            self.assertEqual(command[:3], ["/usr/bin/sandbox-exec", "-p", "unchanged-policy"])
            self.assertEqual(len(kwargs["pass_fds"]), 2)
            for descriptor in kwargs["pass_fds"]:
                self.assertTrue(stat.S_ISFIFO(os.fstat(descriptor).st_mode))
            self.assertNotIn(str(self.run), kwargs["env"].values())
            # Only native policy/kernel observation is stubbed. Main, supervision,
            # subprocess descriptor closing, backpressure and parent storage are real.
            return real_popen([sys.executable, "-B", "-c", helper,
                               *command[command.index("--focused-verifier"):]], **kwargs)
        with patch.object(verifier_parent.subprocess, "run",
                          return_value=SimpleNamespace(stdout=json.dumps(plan).encode())), \
                patch.object(verifier_parent, "supervisor_policy",
                             return_value=("unchanged-policy", Path(sys.executable), Path(graph_process.__file__))), \
                patch.object(verifier_parent.subprocess, "Popen", side_effect=spawn):
            result = runner(0, call_id, threading.Event())
        self.assertTrue(runner.cleanup_pass, result)
        self.assertEqual(result["verifier_output"]["request_id"], "request")
        self.assertEqual(result["verifier_output"]["request_sha256"], "b" * 64)
        self.assertEqual(result["verifier_output"]["verifier_sha256"], caller._canonical_sha256(grant))
        self.assertEqual(result["verifier_output"]["call_id"], call_id)
        frame = {"version": 1, "binding": runner.binding, "call_id": call_id,
                 "verifier_index": 0, "result": result}
        encoded = json.dumps(frame, ensure_ascii=True).encode()
        self.assertLess(len(encoded), 262144)
        self.assertEqual(json.loads(encoded)["result"], result)
        return result

    def test_verifier_output_undrained_reader_preserves_exit_deadline_and_cancellation(self):
        from codex_collaboration_harness import graph_process
        size = 2 * 1024 * 1024
        for mode in ("success", "deadline", "cancel"):
            with self.subTest(mode=mode):
                capture = verifier_parent._VerifierOutput(self.run, self.scratch, {"call_id": mode})
                process = None
                try:
                    suffix = ("g.threading.Timer(.2, lambda: g.os.kill(g.os.getpid(), g.signal.SIGTERM)).start()\n"
                              if mode == "cancel" else "")
                    body = ("import sys,time\n"
                            f"raw=b'x'*{size}\n"
                            "for stream in (sys.stdout.buffer,sys.stderr.buffer):\n"
                            " stream.write(raw)\n stream.flush()\n" +
                            ("time.sleep(30)\n" if mode != "success" else ""))
                    started = time.monotonic()
                    process = subprocess.Popen(
                        [sys.executable, "-B", "-c", self.supervisor_helper(suffix),
                         "--focused-verifier", "--parent-pid", str(os.getpid()),
                         "--timeout", ".2" if mode == "deadline" else "5",
                         "--diagnostic-fds", *(str(fd) for fd in capture.writers)],
                        cwd=self.workspace,
                        env=dict(os.environ, PYTHONPATH=str(Path(graph_process.__file__).resolve().parent.parent)),
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        pass_fds=tuple(capture.writers), start_new_session=True)
                    capture.release_writers()
                    # These readers stay open but completely undrained until the
                    # supervisor exits. communicate drains only its control frame.
                    output, errors = process.communicate(json.dumps({
                        "argv": [sys.executable, "-B", "-c", body], "cwd": str(self.workspace),
                        "diagnostic_identity": capture.identity}).encode(), timeout=4)
                    self.assertLess(time.monotonic() - started, 4)
                    self.assertEqual(process.returncode, 0, errors)
                    self.assertFalse(errors)
                    result = json.loads(output)
                    self.assertTrue(result["process_reaped"] and result["process_group_empty"], result)
                    self.assertEqual(result["success"], mode == "success")
                    self.assertEqual(result["outcome"], "known" if mode == "success" else "unknown")
                    self.assertEqual(result["exit_code"], 0 if mode == "success" else -signal.SIGTERM)
                    for reader in capture.readers:
                        while data := os.read(reader, 65536):
                            capture.append(reader, data)
                        capture.append(reader, b"")
                    observation = result["diagnostic_capture"]
                    reference = capture.finish(observation)["streams"]["stdout"]
                    producer = observation["streams"]["stdout"]
                    self.assertTrue(producer["eof"])
                    self.assertFalse(producer["forwarded"])
                    self.assertGreater(producer["bytes"], reference["bytes"])
                    self.assertTrue(reference["durable"], reference)
                    self.assertFalse(reference["complete"])
                    self.assertEqual(reference["error"], "VERIFIER_OUTPUT_CAPTURE_INCOMPLETE")
                    self.assertEqual(Path(reference["path"]).read_bytes(), b"x" * reference["bytes"])
                    self.assertLess(len(result["stdout"]), 8300)
                    if mode == "success":
                        for producer in observation["streams"].values():
                            self.assertEqual(producer["bytes"], size)
                            self.assertEqual(producer["sha256"], hashlib.sha256(b"x" * size).hexdigest())
                finally:
                    if process is not None and process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                    capture.close()

    def test_verifier_output_pipe_write_failure_cannot_claim_complete(self):
        suffix = ("original_write=g.os.write\n"
                  "diagnostic_fds=set(map(int,g.sys.argv[-2:]))\n"
                  "def fail_write(fd,data):\n"
                  " if fd in diagnostic_fds: raise BrokenPipeError('capture unavailable')\n"
                  " return original_write(fd,data)\n"
                  "g.os.write=fail_write\n")
        result = self.invoke("print('ok')", helper_suffix=suffix)
        self.assertTrue(result["success"])
        self.assertEqual((result["exit_code"], result["outcome"]), (0, "known"))
        self.assertEqual(result["stdout"], "ok\n")
        reference = result["verifier_output"]["streams"]["stdout"]
        self.assertTrue(reference["durable"])
        self.assertFalse(reference["complete"])
        self.assertEqual(reference["bytes"], 0)
        self.assertEqual(reference["error"], "VERIFIER_OUTPUT_CAPTURE_INCOMPLETE")

    def test_verifier_output_original_bytes_middle_marker_and_bounded_excerpt(self):
        raw = b"head\xff\x00\n" + b"x" * 600000 + b"MIDDLE\xfe\x80" + b"y" * 600000 + b"\ntail\n"
        body = ("import os,sys\n"
                "for fd in range(3,256):\n"
                " try: os.fstat(fd)\n"
                " except OSError: continue\n"
                " raise AssertionError('leaked descriptor')\n"
                "raw=b'head\\xff\\x00\\n'+b'x'*600000+b'MIDDLE\\xfe\\x80'+b'y'*600000+b'\\ntail\\n'\n"
                "for stream in (sys.stdout.buffer,sys.stderr.buffer):\n"
                " stream.write(raw)\n stream.flush()\n"
                "sys.exit(1)\n")
        result = self.invoke(body)
        self.assertFalse(result["success"])
        self.assertEqual((result["exit_code"], result["outcome"]), (1, "known"))
        for name in ("stdout", "stderr"):
            reference = result["verifier_output"]["streams"][name]
            self.assertTrue(reference["complete"] and reference["durable"], reference)
            self.assertEqual(reference["bytes"], len(raw))
            self.assertEqual(reference["sha256"], hashlib.sha256(raw).hexdigest())
            path = Path(reference["path"])
            self.assertEqual(path.parent, self.run)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("diagnostic bytes truncated", result[name])
            self.assertNotIn("MIDDLE", result[name])
            self.assertLess(len(result[name]), 8300)

    def test_verifier_output_timeout_preserves_unknown_and_retains_prefix(self):
        result = self.invoke("import sys,time\nsys.stdout.buffer.write(b'partial\\xff')\n"
                             "sys.stdout.flush()\ntime.sleep(30)\n", timeout=.2)
        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "unknown")
        self.assertTrue(result["process_reaped"] and result["process_group_empty"])
        reference = result["verifier_output"]["streams"]["stdout"]
        self.assertTrue(reference["complete"], reference)
        self.assertEqual(Path(reference["path"]).read_bytes(), b"partial\xff")

    def test_verifier_output_partial_disk_write_and_sync_failure_are_truthful(self):
        real_write = os.write
        def short_write(descriptor, data):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                if os.fstat(descriptor).st_size:
                    raise OSError("disk full")
                return real_write(descriptor, data[:3])
            return real_write(descriptor, data)
        with patch.object(verifier_parent.os, "write", side_effect=short_write):
            result = self.invoke("print('abcdef')")
        self.assertTrue(result["success"])
        reference = result["verifier_output"]["streams"]["stdout"]
        self.assertEqual(reference["error"], "VERIFIER_OUTPUT_WRITE_FAILED")
        self.assertFalse(reference["complete"])
        self.assertTrue(reference["durable"])
        self.assertEqual(Path(reference["path"]).read_bytes(), b"abc")
        self.assertEqual(reference["bytes"], 3)
        with patch.object(verifier_parent.os, "fsync", side_effect=OSError("sync failed")):
            result = self.invoke("print('ok')", call_id="sync-failure")
        self.assertTrue(result["success"])
        reference = result["verifier_output"]["streams"]["stdout"]
        self.assertFalse(reference["complete"] or reference["durable"])
        self.assertIsNone(reference["path"])

    def test_verifier_output_symlink_and_existing_leaf_never_redirect_writes(self):
        alias = self.root / "alias"
        alias.symlink_to(self.run, target_is_directory=True)
        result = self.invoke("print('ok')", root=alias)
        self.assertTrue(result["success"])
        for reference in result["verifier_output"]["streams"].values():
            self.assertEqual(reference["error"], "VERIFIER_OUTPUT_PATH_REJECTED")
            self.assertIsNone(reference["path"])
            self.assertFalse(reference["complete"])
        self.assertEqual(list(self.run.iterdir()), [])
        original = self.invoke("print('ok')")
        target = self.root / "untouched"
        target.write_bytes(b"safe")
        path = Path(original["verifier_output"]["streams"]["stdout"]["path"])
        path.unlink()
        path.symlink_to(target)
        result = self.invoke("print('ok')")
        self.assertEqual(target.read_bytes(), b"safe")
        self.assertEqual(result["verifier_output"]["streams"]["stdout"]["error"],
                         "VERIFIER_OUTPUT_CREATE_FAILED")

    def test_verifier_output_missing_or_foreign_producer_cannot_claim_complete(self):
        for suffix in (
                "original=g._DiagnosticCapture.observation\n"
                "def incomplete(self): return dict(original(self), eof=False)\n"
                "g._DiagnosticCapture.observation=incomplete\n",
                "original=g._DiagnosticCapture.observation\n"
                "def incomplete(self): return dict(original(self), bytes=self.total+1)\n"
                "g._DiagnosticCapture.observation=incomplete\n"):
            result = self.invoke("print('ok')", call_id=hashlib.sha256(suffix.encode()).hexdigest(),
                                 helper_suffix=suffix)
            self.assertTrue(result["success"])
            reference = result["verifier_output"]["streams"]["stdout"]
            self.assertTrue(reference["durable"])
            self.assertFalse(reference["complete"])
        identity = {"request_id": "request", "verifier_sha256": "a" * 64, "call_id": "call"}
        capture = verifier_parent._VerifierOutput(self.run, self.scratch, identity)
        try:
            for reader in capture.readers:
                capture.append(reader, b"")
            foreign = {"identity": dict(identity, call_id="foreign"), "streams": {
                name: {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(),
                       "eof": True, "forwarded": True} for name in ("stdout", "stderr")}}
            reference = capture.finish(foreign)
            self.assertFalse(any(row["complete"] for row in reference["streams"].values()))
        finally:
            capture.close()

    def test_verifier_output_directory_replacement_is_not_a_durable_reference(self):
        capture = verifier_parent._VerifierOutput(self.run, self.scratch, {"call_id": "rename"})
        try:
            moved = self.root / "moved"
            self.run.rename(moved)
            self.run.mkdir()
            reference = capture.finish(None)
            for row in reference["streams"].values():
                self.assertFalse(row["complete"] or row["durable"])
                self.assertIsNone(row["path"])
        finally:
            capture.close()


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

    def runner(self, body, timeout=5, *, python_import_roots=None, read_scopes=(), cwd=None,
               verifier_only=False, optional_create=False):
        entry = self.root / "acceptance.py" if verifier_only else self.script
        entry.write_text(body)
        pin = {"path":str(entry),"sha256":caller._file_sha256(entry)}
        if verifier_only:
            pin["scope"] = "verifier_only"
        action = {
            "mission":{"mission_id":"parent-verifier-tests","task_id":"exact-fixture",
                       "mode":"DELIVERY","objective":"Exercise scoped public verifier",
                       "current_predicate":"Parent channel acceptance"},
            "context_summary":"Disposable fixture; no provider send or production mutation.",
            "operations":["read","command","modify"],
            "read_scopes":([] if verifier_only else ["verify.py"]) + ["solution.txt", *read_scopes],
            "write_scopes":["solution.txt"],"target_paths":["solution.txt"],
            "command_templates":[], "source_read":True,
            "forbidden_effects":["network","delete"],
            "verifier_commands":[{"argv":[str(self.python),str(entry)],
                "executable_sha256":caller._file_sha256(self.python),
                "pinned_files":[pin],
                "timeout_seconds":timeout,"scratch_root":str(self.scratch),"network":False}],
        }
        if python_import_roots is not None:
            action["verifier_commands"][0]["python_import_roots"] = list(python_import_roots)
        if cwd is not None:
            action["verifier_commands"][0]["cwd"] = str(cwd)
        if optional_create:
            action["operations"].append("create")
            for key in ("read_scopes", "write_scopes", "target_paths"):
                action[key].append("optional.txt")
        # Verifier fixtures run against the installed or candidate router alike, so they bind no
        # V431 parent shell (test_parent_shell covers it).
        _, contract = local_context.compile_context(self.workspace, action, artifact_root=self.artifacts,
                                                    parent_shell=False)
        return ParentVerifier(self.workspace, contract, self.router, time.monotonic()+20)

    def test_real_verifier_binds_an_admitted_target_that_remains_absent(self):
        runner = self.runner("from pathlib import Path\nassert not Path('optional.txt').exists()\n",
                             optional_create=True)
        self.assertIsNone(runner.contract["dcf_generation"]["source_snapshot"]["optional.txt"])
        result = runner(0, "optional-absent", threading.Event())
        self.assertTrue(result["success"], result)
        self.assertIsNone(result["verification_evidence"]["source_postimages"]["optional.txt"])
        self.assertFalse((self.workspace / "optional.txt").exists())
        self.assertTrue(runner.cleanup_pass)

    def test_real_verifier_only_external_entry_keeps_scratch_only_writes(self):
        runner = self.runner(
            "import os\nfrom pathlib import Path\n"
            "assert Path('solution.txt').read_text() == 'broken'\n"
            "for target in (Path('solution.txt'), Path(__file__)):\n"
            " try:\n  target.write_text('forbidden')\n"
            " except PermissionError:\n  pass\n"
            " else:\n  raise AssertionError('verifier_only granted write access')\n"
            "Path(os.environ['TMPDIR'], 'external-proof.txt').write_text('verified')\n"
            "print('external-entry-confirmed')\n",
            verifier_only=True,
        )
        pin, = runner.contract["verifier_commands"][0]["pinned_files"]
        self.assertEqual(pin["scope"], "verifier_only")
        self.assertEqual(runner.contract["read_scopes"], ["solution.txt"])
        self.assertFalse(Path(pin["path"]).is_relative_to(self.workspace))
        result = runner(0, "external-entry", threading.Event())
        self.assertTrue(result["success"], result)
        self.assertIn("external-entry-confirmed", result["stdout"])
        self.assertEqual((self.scratch / "external-proof.txt").read_text(), "verified")
        self.assertEqual(self.solution.read_text(), "broken")
        self.assertEqual(caller._file_sha256(Path(pin["path"])), pin["sha256"])
        self.assertTrue(result["process_reaped"] and result["process_group_empty"], result)
        self.assertTrue(runner.cleanup_pass)

    def test_real_cwd_matches_acceptance_without_granting_writes(self):
        acceptance_dir = self.root / "acceptance"
        acceptance_dir.mkdir()
        runner = self.runner(
            "import os\nfrom pathlib import Path\n"
            f"assert Path.cwd() == Path({str(acceptance_dir)!r})\n"
            "try:\n Path('must-not-write').write_text('forbidden')\n"
            "except PermissionError:\n pass\n"
            "else:\n raise AssertionError('cwd granted write access')\n"
            "Path(os.environ['TMPDIR'], 'cwd-proof.txt').write_text(str(Path.cwd()))\n"
            "print('acceptance-cwd-confirmed')\n",
            cwd=acceptance_dir,
        )
        result = runner(0, "acceptance-cwd", threading.Event())
        self.assertTrue(result["success"], result)
        self.assertIn("acceptance-cwd-confirmed", result["stdout"])
        self.assertEqual((self.scratch / "cwd-proof.txt").read_text(), str(acceptance_dir))
        self.assertFalse((acceptance_dir / "must-not-write").exists())
        self.assertTrue(runner.cleanup_pass)

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

    def test_real_import_roots_enable_module_resolution_with_global_source_reads(self):
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
            f"assert Path({str(sibling)!r}).read_text() == 'unadmitted sibling'\n"
            f"assert Path({str(outside)!r}).read_text() == 'outside workspace'\n"
            "print('module-resolved', flush=True)\n")
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
        self.assertIn("module-resolved", result["stdout"])
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

    def test_real_scope_reads_sources_but_rejects_writes_and_existing_services(self):
        import socket

        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        secret = self.root / "outside.txt"
        secret.write_text("outside")
        runner = self.runner(
            "from pathlib import Path\nimport socket\n"
            f"scratch=Path({str(self.scratch)!r})\n"
            "(scratch/'allowed').write_text('yes')\n"
            f"assert Path({str(secret)!r}).read_text() == 'outside'\n"
            "for label, action in ["
            "('workspace_write', lambda:Path('solution.txt').write_text('bad')), "
            f"('existing_service', lambda:socket.socket().connect(('127.0.0.1',{port})))]:\n"
            " try: action()\n except PermissionError: pass\n else: raise AssertionError('escaped scope: '+label)\n"
            "print('confined')\n")
        with patch.object(verifier_parent, "supervisor_policy", wraps=verifier_parent.supervisor_policy) as observed:
            result = runner(0, "scope", threading.Event())
        profile = observed.call_args.args[0]
        self.assertTrue(result["success"], {**result, "listener_port": port,
            "listener_in_profile": f'(remote ip "localhost:{port}")' in profile,
            "profile_sha256": hashlib.sha256(profile.encode()).hexdigest()})
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


def _paired_service_prefix(binary, workspace, scratch, read_scopes):
    """One existing OS fence per disposable service; the parent owns the channel."""
    settings = graph_entry.GraphSettings(binary, caller._file_sha256(binary), workspace, scratch)
    profile = graph_entry.sandbox_profile(
        {"jspace": {"read_scopes": read_scopes, "write_scopes": []}}, settings, scratch)
    profile, _, _ = supervisor_policy(profile)
    # ReceiptStore opens each ancestor with O_NOFOLLOW before opening its root.
    parents = set(workspace.resolve().parents) | set(scratch.resolve().parents)
    profile += '\n(allow file-read-data ' + ' '.join(
        f'(require-all (literal {json.dumps(str(path))}) (vnode-type DIRECTORY))'
        for path in sorted(parents)) + ')\n'
    profile += ('\n(deny file-read* file-write* (regex #"/auth[.]json$"))\n'
                '(allow network-inbound (local ip "localhost:*"))\n'
                '(allow network-outbound (remote ip "localhost:*"))\n')
    return ["/usr/bin/sandbox-exec", "-p", profile, str(binary)]


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("NOKIY_VERIFIER_TEST_ROUTER")
                     and os.environ.get("NOKIY_NATIVE_SANDBOX_TEST_BIN"),
                     "requires parent-native paired wire execution")
class PairedVerifierWireTests(unittest.TestCase):
    """Real producer -> inherited channel -> Rust command consumer; no inference entry point."""

    def test_candidate_roundtrip(self):
        from codex_collaboration_harness import full_core
        from codex_collaboration_harness.verifier_channel import VerifierChannel

        identity = {key: os.environ[key] for key in ("HOME", "CODEX_HOME") if key in os.environ}
        f = VerifierParentTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        binding_path = os.environ.get("NOKIY_PAIRED_WIRE_BINDING")
        receipt_path = os.environ.get("NOKIY_PAIRED_WIRE_RECEIPT")
        if binding_path:
            bound = json.loads(Path(binding_path).read_text())
            self.assertEqual(Path(caller.__file__).resolve().parent.parent, Path(bound["package_root"]))
            for row, actual in ((bound["fixture"], Path(__file__).resolve()),
                                (bound["native_cli"], Path(os.environ["NOKIY_NATIVE_SANDBOX_TEST_BIN"]).resolve()),
                                (bound["runtime_artifacts"]["tura_router"], f.router.path)):
                self.assertEqual(row, {"path": str(actual), "sha256": caller._file_sha256(actual)})
        raw = b"head\xff\x00\n" + b"x" * 600000 + b"MIDDLE\xfe\x80" + b"y" * 600000 + b"\ntail\n"
        planned = f.runner("import sys\n"
            "raw=b'head\\xff\\x00\\n'+b'x'*600000+b'MIDDLE\\xfe\\x80'+b'y'*600000+b'\\ntail\\n'\n"
            "for stream in (sys.stdout.buffer,sys.stderr.buffer):\n stream.write(raw)\n stream.flush()\n")
        output_root = Path(receipt_path).parent / "paired-wire-output" if receipt_path else f.artifacts / "output"
        output_root.mkdir(mode=0o700)
        runner = ParentVerifier(f.workspace, planned.contract, f.router, time.monotonic() + 45,
            diagnostic_root=output_root, request_id="paired-wire", request_sha256=caller._canonical_sha256(planned.contract))
        produced = []

        def handle(index, call_id, cancelled):
            result = runner.handle_request(index, call_id, cancelled)
            produced.append(result)
            return result  # The real reply, including verifier_output, without any schema projection.

        state = f.artifacts / "state"
        state.mkdir()
        provider = state / "offline-provider.json"
        provider.write_text("{}")
        runtime = SimpleNamespace(runtime_root=f.router.path.parent, artifacts={
            "tura_exec": f.router, "provider_config": SimpleNamespace(path=provider)})
        request = SimpleNamespace(workspace=f.workspace, timeout_seconds=30,
                                  terminal_delivery="evidence_only", initial_task_state=None)
        database = (Path(bound["runtime_artifacts"]["tura_session_db"]["path"]) if binding_path else
                    Path(os.environ.get("NOKIY_VERIFIER_TEST_SESSION_DB", str(f.router.path.with_name("tura_session_db")))))
        # Preserve the invoking identity; generated fixtures remain in scratch.
        clean_env = {**identity,
                     "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        with patch.dict(os.environ, clean_env, clear=True):
            env = full_core._environment(request, runtime, state)
            self.assertEqual({key: env[key] for key in ("HOME", "CODEX_HOME") if key in env}, identity)
            with VerifierChannel(runner.binding, 1, handle, timeout_seconds=45) as channel:
                children, logs = [], []
                try:
                    for binary, arguments, leaf in ((database, [], "service.addr"),
                                                   (f.router.path, ["serve-socket"], "router.addr")):
                        log_path = f.root / (leaf + ".log")
                        log = log_path.open("xb")
                        logs.append(log)
                        service_env = dict(env)
                        service_env["TMPDIR"] = str(f.scratch)
                        fds = (channel.router_fd,) if arguments else ()
                        if fds:
                            service_env["NOKIY_VERIFIER_FD"] = str(fds[0])
                        process = subprocess.Popen([*_paired_service_prefix(
                            binary, f.workspace, f.artifacts, runner.contract["read_scopes"]), *arguments],
                            cwd=f.workspace, env=service_env, pass_fds=fds,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                        children.append(process)
                        if fds:
                            channel.release_router_copy()
                        marker = state / "session_log" / leaf
                        deadline = time.monotonic() + 15
                        while not marker.is_file() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(.02)
                        self.assertTrue(marker.is_file(), log_path.read_text(errors="replace"))
                    host, port = json.loads(marker.read_text())["addr"].rsplit(":", 1)
                    payload = {"session_id": "paired-wire", "runtime_id": "paired-wire",
                        "session_directory": str(f.workspace), "arguments": {"execution_id": "paired-wire",
                        "commands": [{"command_type": "focused_verifier", "command_line": '{"verifier_index":0}',
                            "command_id": "paired-wire-0", "command_run_id": "paired-wire",
                            "provider_tool_call_id": "paired-wire", "command_index": 0, "step": 1}]},
                        "allowed_commands": ["focused_verifier"], "jspace_contract": runner.contract, "sandbox": True}
                    with socket.create_connection((host, int(port)), timeout=45) as connection:
                        connection.sendall((json.dumps({"request_id": "paired-wire", "kind": "call",
                            "method": "execution.command_run", "payload": payload}) + "\n").encode())
                        with connection.makefile("rb") as stream:
                            response = json.loads(stream.readline(1048576))
                finally:
                    for process in reversed(children):
                        if process.poll() is None:
                            process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    for log in logs:
                        log.close()
                self.assertIsNone(channel.failure)
        self.assertEqual({key: os.environ[key] for key in ("HOME", "CODEX_HOME") if key in os.environ}, identity)
        self.assertTrue(response["ok"], response)
        self.assertEqual(runner.calls, 1)
        self.assertTrue(runner.cleanup_pass)
        self.assertEqual(len(produced), 1)
        producer = produced[0]
        consumed = response["payload"]["result"]["results"][0]
        self.assertTrue(consumed["success"], consumed)  # V355 rejects the real verifier_output here.
        output = consumed["output"]
        self.assertEqual((producer["success"], producer["exit_code"], producer["outcome"]), (True, 0, "known"))
        receipt = output["terminal_receipt"]
        self.assertEqual(receipt["outcome"], "known")
        self.assertTrue(receipt["process_reaped"] and receipt["process_group_empty"])
        self.assertEqual(output["exit_code"], 0)
        for name in ("stdout", "stderr", "verifier_output"):
            self.assertEqual(output[name], producer[name])
        for name in ("stdout", "stderr"):
            self.assertLess(len(output[name]), 8300)
            self.assertIn("diagnostic bytes truncated", output[name])
            self.assertNotIn("MIDDLE", output[name])
            row = output["verifier_output"]["streams"][name]
            self.assertTrue(row["complete"] and row["durable"], row)
            self.assertEqual(row["bytes"], len(raw))
            self.assertEqual(row["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(Path(row["path"]).parent, output_root)
            self.assertEqual(Path(row["path"]).read_bytes(), raw)
        self.assertEqual(f.solution.read_text(), "broken")
        if receipt_path:
            self.assertIsNotNone(binding_path)
            with Path(receipt_path).open("x") as stream:
                json.dump({"binding": {"path": binding_path, "sha256": caller._file_sha256(Path(binding_path))},
                    "test": "test_verifier_parent.PairedVerifierWireTests.test_candidate_roundtrip",
                    "passed": True, "execution_reused": False, "result": producer}, stream, sort_keys=True)
