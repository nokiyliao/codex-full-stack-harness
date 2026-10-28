# SPDX-License-Identifier: MIT
"""Opt-in native Seatbelt compatibility checks using disposable processes only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_collaboration_harness.graph_process import DarwinProcesses
import test_full_core
import test_responses_evidence_handoff as handoff
from codex_collaboration_harness import embedded_nokiy as caller, graph_process, local_context


NATIVE = os.environ.get("NOKIY_NATIVE_SANDBOX_TEST_BIN")
MODULE_PATH = str(Path(graph_process.__file__).resolve())
# Native launch sanitizes PYTHONPATH; bind the exact source/candidate under test.
BOUND_IMPORT = (
    f"import sys;sys.path.insert(0,{str(Path(MODULE_PATH).parents[1])!r})\n"
    "from pathlib import Path\nfrom codex_collaboration_harness import graph_process\n"
    f"assert str(Path(graph_process.__file__).resolve())=={MODULE_PATH!r}\n"
)
ROOT_SUPERVISOR = BOUND_IMPORT + """
import dataclasses,json,os,signal,sys,threading
from codex_collaboration_harness.graph_process import inherited_root_parent,supervise
assert inherited_root_parent()==os.getppid()
cancelled=threading.Event()
for sig in (signal.SIGTERM,signal.SIGINT):
    signal.signal(sig,lambda *_:cancelled.set())
result=supervise([sys.executable,'-B',sys.argv[1]],cwd=os.getcwd(),env=dict(os.environ),
    input_bytes=b'',timeout=4,cancelled=cancelled,max_stdout=4096,max_stderr=4096,
    parent_pid=os.getppid(),terminate_grace=.1)
print(json.dumps(dataclasses.asdict(result),default=lambda value:value.decode()))
"""


@unittest.skipUnless(sys.platform == "darwin" and NATIVE, "opt in to actual native Codex sandbox")
class NativeSandboxScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="native-scope-", dir=os.environ.get("NOKIY_GRAPH_TEST_ROOT"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.engine = self.root / "engine.py"
        self.prefix = [str(Path(NATIVE).resolve(strict=True)), "sandbox", "--permission-profile",
            "nokiy_benchmark", "--include-managed-config",
            "-c", 'default_permissions="nokiy_benchmark"',
            "-c", 'permissions.nokiy_benchmark.extends=":workspace"',
            "-c", "permissions.nokiy_benchmark.network.enabled=true", "--"]

    def test_verifier_preflight_reads_native_membership_without_a_worker(self):
        code = BOUND_IMPORT + """
import json
from codex_collaboration_harness import full_core
assert graph_process.current_process_is_sandboxed() is True
assert full_core.verifier_dispatch_blocker({}) is None
blocker=full_core.verifier_dispatch_blocker({'verifier_commands':[{}]})
assert blocker=='NOKIY_FULL_CORE_VERIFIER_OUTER_SCOPE_REQUIRED'
print(json.dumps({'blocker':blocker,'provider_calls':0,'worker_started':False}))
"""
        result = subprocess.run([*self.prefix, sys.executable, "-I", "-B", "-c", code],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["provider_calls"], 0)

    def launch(self, body):
        self.engine.write_text(body)
        process = subprocess.Popen([*self.prefix, sys.executable, "-I", "-B", "-c",
            ROOT_SUPERVISOR, str(self.engine)], cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.settle, process)
        return process

    @staticmethod
    def settle(process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)

    def outcome(self, process):
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertTrue(result["scope"]["engine_reaped"], result)
        self.assertTrue(result["scope"]["no_live_descendants"], result)
        self.assertIsNone(result["scope"]["cleanup_error"], result)
        return result

    def test_root_scope_reaps_session_escape_without_touching_sibling_or_parent(self):
        outside = subprocess.Popen(["/bin/sleep", "10"])
        sibling = subprocess.Popen([*self.prefix, "/bin/sleep", "10"], cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.settle, outside)
        self.addCleanup(self.settle, sibling)
        result = self.outcome(self.launch(
            "import json,subprocess,sys\n"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'],"
            "start_new_session=True)\nprint(json.dumps({'child':p.pid}))\n"))
        self.assertEqual(result["failure"], "GRAPH_DESCENDANTS_OUTLIVED_ENGINE", result)
        child = json.loads(result["stdout"])["child"]
        info = DarwinProcesses().info(child)
        self.assertTrue(info is None or info.status == 5)
        self.assertGreaterEqual(result["scope"]["descendant_signals"], 1)
        self.assertIsNone(outside.poll(), "outside process was interrupted")
        self.assertIsNone(sibling.poll(), "independent native scope was interrupted")

    def test_same_scope_child_cannot_claim_root_ownership(self):
        code = """
import subprocess,sys
p=subprocess.run([sys.executable,'-I','-B','-c',sys.argv[1]],capture_output=True,text=True)
assert p.returncode != 0 and 'GRAPH_SUPERVISOR_SIGNAL_ISOLATION_REQUIRED' in p.stderr
print('child root claim rejected')
"""
        probe = BOUND_IMPORT + "graph_process.inherited_root_parent()"
        result = subprocess.run([*self.prefix, sys.executable, "-I", "-B", "-c", code, probe],
            cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_scope_member_prevents_reuse_without_signalling_it(self):
        code = BOUND_IMPORT + """
import subprocess
from codex_collaboration_harness.graph_process import inherited_root_parent,ProcessScopeError
p=subprocess.Popen(['/bin/sleep','10'])
try:
    try: inherited_root_parent()
    except ProcessScopeError as error: assert str(error)=='GRAPH_SUPERVISOR_SCOPE_NOT_EXCLUSIVE'
    else: raise AssertionError('busy scope admitted')
    assert p.poll() is None
finally:
    p.terminate();p.wait()
"""
        result = subprocess.run([*self.prefix, sys.executable, "-I", "-B", "-c", code],
            cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_parent_exit_cleans_the_owned_engine(self):
        process = self.launch("from pathlib import Path\nimport os,time\n"
                              "Path('ready').write_text(str(os.getpid()))\ntime.sleep(10)\n")
        marker = self.root / "ready"
        deadline = time.monotonic() + 5
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists(), "native fixture did not start")
        pid = int(marker.read_text())
        process.kill()
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, -signal.SIGKILL, stderr)
        result = json.loads(stdout)
        self.assertEqual(result["failure"], "GRAPH_SUPERVISOR_CALLER_EXITED", result)
        self.assertTrue(result["scope"]["engine_reaped"], result)
        self.assertTrue(result["scope"]["no_live_descendants"], result)
        info = DarwinProcesses().info(pid)
        self.assertTrue(info is None or info.status == 5)


@unittest.skipUnless(sys.platform == "darwin" and NATIVE, "opt in to actual native Codex sandbox")
class NativeFullCoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_full_core.FullCoreTests()
        with patch.object(tempfile, "tempdir", os.environ.get("NOKIY_GRAPH_TEST_ROOT")):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.prefix = [str(Path(NATIVE).resolve(strict=True)), "sandbox", "--permission-profile",
            "nokiy_benchmark", "--include-managed-config",
            "-c", 'default_permissions="nokiy_benchmark"',
            "-c", 'permissions.nokiy_benchmark.extends=":workspace"',
            "-c", "permissions.nokiy_benchmark.network.enabled=true", "--"]

    def launch(self, *, shell=False):
        code = ("import os;from pathlib import Path;"
                "Path('caller.pid').write_text(str(os.getpid()));"
                "from codex_collaboration_harness.embedded_nokiy import main;raise SystemExit(main())")
        command = [sys.executable, "-B", "-c", code, "run", "--request", str(self.f.request_path)]
        if shell:
            command = ["/bin/zsh", "-lc", shlex.join(command)]
        process = subprocess.Popen([*self.prefix, *command], cwd=self.f.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(NativeSandboxScopeTests.settle, process)
        return process

    def test_native_shell_projection_retains_command_root(self):
        process = self.launch(shell=True)
        stdout, stderr = process.communicate(timeout=25)
        self.assertEqual(process.returncode, 0, stderr + stdout)
        result = json.loads(stdout)
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)
        self.assertTrue(result["cleanup_pass"], result)
        self.assertEqual(result["cleanup"]["isolation_source"], "native_command_root")
        self.assertEqual(result["result_inspection"]["status"], "EVIDENCE_VERIFIED", result)
        self.assertEqual(result["result_inspection"]["cleanup"]["supervisor_pid"], "current_root_settled")

    def test_real_native_caller_finishes_and_same_request_recovery_does_not_reexecute(self):
        request = self.fixture._request()
        process = self.launch()
        stdout, stderr = process.communicate(timeout=25)
        self.assertEqual(process.returncode, 0, stderr + stdout)
        result = json.loads(stdout)
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)
        self.assertTrue(result["cleanup_pass"], result)
        self.assertEqual(result["cleanup"]["isolation_source"], "native_command_root")
        self.assertEqual((self.f.workspace / "answer.txt").read_text(), "full-core result\n")
        run = request.artifact_root / request.request_id
        before = {name: (run / name).read_bytes() for name in
                  ("terminal.json", "core.jsonl", "execution-state/cli-reached")}
        # Recovery remains available even from a child that cannot own the fence.
        child = [sys.executable, "-B", "-m", "codex_collaboration_harness.embedded_nokiy",
                 "run", "--request", str(self.f.request_path)]
        code = "import json,subprocess,sys; p=subprocess.run(json.loads(sys.argv[1]));sys.exit(p.returncode)"
        recovered = subprocess.run([*self.prefix, sys.executable, "-B", "-c", code, json.dumps(child)],
            cwd=self.f.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(recovered.returncode, 0, recovered.stderr + recovered.stdout)
        self.assertEqual(json.loads(recovered.stdout)["request_id"], request.request_id)
        for name, contents in before.items():
            self.assertEqual((run / name).read_bytes(), contents)

    def test_native_caller_cancel_reaps_escaped_worker_and_preserves_terminal_failure(self):
        request = self.fixture._request(prompt="TIMEOUT")
        process = self.launch()
        run = request.artifact_root / request.request_id
        marker = run / "execution-state/grandchild.pid"
        deadline = time.monotonic() + 5
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists(), "fixture did not start")
        caller_pid = int((self.f.root / "caller.pid").read_text())
        child_pid = int(marker.read_text())
        os.kill(caller_pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=25)
        self.assertEqual(process.returncode, 2, stderr + stdout)
        result = json.loads(stdout)
        self.assertEqual(result["first_typed_blocker"], "NOKIY_EMBEDDED_CANCELLED", result)
        self.assertTrue(result["cleanup_pass"], result)
        self.assertEqual(result["cleanup"]["isolation_source"], "native_command_root")
        info = DarwinProcesses().info(child_pid)
        self.assertTrue(info is None or info.status == 5)
        self.assertFalse((self.f.workspace / "answer.txt").exists())


class CompiledRootScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = handoff.RuntimeEvidenceHandoffTests()
        with patch.object(tempfile, "tempdir", os.environ.get("NOKIY_GRAPH_TEST_ROOT")):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f

    def test_compiled_read_delivery_and_recovery_keep_root_scope(self):
        self._assert_compiled_delivery(native=False)

    @unittest.skipUnless(NATIVE, "opt in to installed caller through actual native CLI")
    def test_installed_caller_compiled_delivery_through_native_cli(self):
        self._assert_compiled_delivery(native=True)

    def test_compiled_legacy_answer_requires_final_provider_turn(self):
        self._assert_compiled_delivery(native=False, delivery="legacy_answer")

    def test_compiled_explicit_answer_skips_regeneration_and_recovers(self):
        self._assert_compiled_delivery(native=False, delivery="explicit_answer")

    def test_compiled_digest_format_rejection_then_corrected_read(self):
        self._assert_compiled_delivery(native=False, delivery="explicit_answer", malformed_hash=True)

    @unittest.skipUnless(NATIVE, "opt in to installed caller through actual native CLI")
    def test_installed_caller_digest_format_rejection_then_corrected_read(self):
        self._assert_compiled_delivery(native=True, delivery="explicit_answer", malformed_hash=True)

    def test_compiled_long_explicit_answer_is_lossless(self):
        self._assert_compiled_delivery(native=False, delivery="explicit_answer", long_answer=True)

    @unittest.skipUnless(NATIVE, "opt in to installed caller through actual native CLI")
    def test_installed_caller_long_explicit_answer_is_lossless(self):
        self._assert_compiled_delivery(native=True, delivery="explicit_answer", long_answer=True)

    def test_compiled_complete_answer_in_first_summary(self):
        self._assert_compiled_delivery(native=False, delivery="explicit_answer",
                                       long_answer=True, inline_answer=True)

    @unittest.skipUnless(NATIVE, "opt in to installed caller through actual native CLI")
    def test_installed_caller_complete_answer_in_first_summary(self):
        self._assert_compiled_delivery(native=True, delivery="explicit_answer",
                                       long_answer=True, inline_answer=True)

    @unittest.skipUnless(NATIVE, "opt in to installed caller through actual native CLI")
    def test_installed_caller_explicit_answer_through_native_cli(self):
        self._assert_compiled_delivery(native=True, delivery="explicit_answer")

    def _assert_compiled_delivery(self, *, native, delivery="evidence_only", long_answer=False,
                                  inline_answer=False, malformed_hash=False):
        if (delivery == "explicit_answer" and
                b"explicit terminal reply published and persisted" not in
                self.fixture.paths["tura_runtime"].read_bytes()):
            self.skipTest("selected runtime predates explicit terminal answer delivery")
        if (long_answer and b"persisted without tool-log truncation" not in
                self.fixture.paths["tura_runtime"].read_bytes()):
            self.skipTest("selected runtime predates lossless terminal answer callbacks")
        if (malformed_hash and b"invalid_expected_sha256" not in
                self.fixture.paths["tura_router"].read_bytes()):
            self.skipTest("selected router predates typed digest-format rejection")
        source = self.f.workspace / "answer.txt"
        source.write_text("native root source evidence\n")
        before = handoff.file_sha256(source)
        answer = json.dumps({"answer": "native root source evidence", "source_sha256": before,
                             "limitations": ["loopback fixture; not a model quality benchmark"]})
        if long_answer:
            answer = json.dumps({"answer": "native root source evidence", "source_sha256": before,
                "findings": [{"id": index, "answer": ("caf\u00e9 \u8b49\u64da " * (64 if inline_answer else 128)) + str(index)}
                             for index in range(12)]}, ensure_ascii=False, indent=2)
            self.assertGreater(len(answer.encode("utf-8")), 8192 if inline_answer else 16_384)
        commands = [handoff._command("source_read", json.dumps({"path": "answer.txt",
            "start_line": 1, "end_line": 1, "expected_sha256": before}), 1),
            handoff._command("task_status", json.dumps({"status": "done"}), 2)]
        messages = [handoff._tool_message(commands, 0)]
        if delivery != "evidence_only":
            status = {"status": "done"}
            if delivery == "explicit_answer":
                status["final_reply"] = answer

            def deliver_after_read(body):
                projected = json.dumps(body["input"])
                if "native root source evidence" not in projected or before not in projected:
                    raise ValueError("terminal answer requested before observing source evidence")
                return handoff._tool_message([
                    handoff._command("task_status", json.dumps(status), 1)], 2 if malformed_hash else 1)

            messages = [handoff._tool_message(commands[:1], 0), deliver_after_read]
            if delivery == "legacy_answer":
                messages.append({"id": "msg-final", "type": "message", "role": "assistant",
                    "phase": "final_answer", "status": "completed",
                    "content": [{"type": "output_text", "text": answer}]})
        if malformed_hash:
            malformed = "5c7d6faa308bb6125375d6b1b06a0106d5e09fd22b182d51a6bf1c6af1c6af1c6af5321124"
            rejected = handoff._command("source_read", json.dumps({"path": "answer.txt",
                "start_line": 1, "end_line": 1, "expected_sha256": malformed}), 1)

            def correct_after_rejection(body):
                self.assertIn("invalid_expected_sha256", json.dumps(body["input"]))
                return handoff._tool_message(commands[:1], 1)

            messages = [handoff._tool_message([rejected], 0), correct_after_rejection, *messages[1:]]
        expected_requests = len(messages)
        run_process = caller._run_process
        with handoff._loopback_provider(messages) as (url, requests, errors, _), \
                handoff._offline_runtime_environment(self.fixture.native_environment, url) as fences:
            self.fixture._loopback_image(url)
            action = {"mission": {"mission_id": "native-root", "task_id": "compiled-source-read",
                "mode": "DELIVERY", "objective": "Read the exact fixture source and finish",
                "current_predicate": "compiled command-root delivery unverified"},
                "context_summary": "Disposable source; loopback provider; no cloud inference.",
                "operations": ["read", "command"], "source_read": True,
                "read_scopes": ["answer.txt"], "write_scopes": [], "target_paths": [],
                "command_templates": [], "forbidden_effects": ["network", "delete", "modify"]}
            capsule, contract = local_context.compile_context(self.f.workspace, action,
                                                              artifact_root=self.f.artifacts)
            self.f.context.write_text(json.dumps(capsule))
            self.f.jspace.write_text(json.dumps(contract))
            request_path = self.f._write_request(prompt="Read answer.txt then finish.")
            value = json.loads(request_path.read_text())
            value.update(execution_profile="direct", authority_effect="none", model=handoff.MODEL,
                         reasoning_effort="max", service_tier="default", timeout_seconds=30,
                         terminal_delivery="evidence_only" if delivery == "evidence_only" else "assistant_reply",
                         initial_task_state={"task_group": "native root fixture", "task_type": ["debug"]})
            if long_answer:
                value["max_trajectory_bytes"] = 4 * 1024 * 1024
            request_path.write_text(json.dumps(value))
            request = caller.load_request(request_path)
            # Explicitly synthetic login for this child only; never copy native state.
            synthetic_home = self.f.root / "synthetic-codex-home"
            synthetic_home.mkdir(mode=0o700)
            synthetic_auth = synthetic_home / "auth.json"
            synthetic_auth.write_text(json.dumps({"tokens": {
                "access_token": handoff.DUMMY_API_KEY,
                "account_id": handoff.DUMMY_ACCOUNT_ID,
            }}))
            synthetic_auth.chmod(0o600)
            child_env = dict(os.environ, CODEX_HOME=str(synthetic_home))
            command = ["/usr/bin/sandbox-exec", "-p", handoff.NATIVE_SIGNAL_FENCE,
                sys.executable, "-B", "-m", "codex_collaboration_harness.embedded_nokiy",
                "run", "--request", str(request_path), "--summary"]
            if native:
                entry = (Path.home() / ".local/bin/nokiy-embedded-run").resolve(strict=True)
                auth = Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")) / "auth.json"
                denied = sorted({str(auth), str(auth.resolve(strict=True))})
                permissions = ["-c", 'default_permissions="nokiy_benchmark"',
                    "-c", 'permissions.nokiy_benchmark.extends=":workspace"',
                    "-c", "permissions.nokiy_benchmark.network.enabled=true"]
                filesystem = "{" + ",".join(json.dumps(path) + '="deny"' for path in denied) + "}"
                permissions += ["-c", "permissions.nokiy_benchmark.filesystem=" + filesystem]
                # Use the installed caller, not PYTHONPATH-dependent editable source.
                # All runtime routes in the verified fixture image point to loopback.
                native_settings = {
                    "CODEX_THREAD_ID": self.fixture.native_environment["CODEX_THREAD_ID"],
                    "CODEX_HOME": str(synthetic_home),
                    "OPENAI_API_KEY": handoff.DUMMY_API_KEY,
                    "OPENAI_ACCOUNT_ID": handoff.DUMMY_ACCOUNT_ID,
                    "OPENAI_TOKEN_EXPIRES": "4102444800000",
                    "OPENAI_CODEX_ENDPOINT": url + handoff.CODEX_RESPONSES_PATH,
                    "NO_PROXY": "127.0.0.1,localhost",
                }
                code_body = """
import errno,json,os,sys
os.environ.update(json.loads(sys.argv[2]))
from codex_collaboration_harness.embedded_nokiy import main
for path in sys.argv[3:]:
    try:
        fd=os.open(path,os.O_RDONLY)
    except OSError as error:
        assert error.errno in (errno.EACCES,errno.EPERM), error
    else:
        os.close(fd)
        raise AssertionError('real credential read was not denied')
raise SystemExit(main(['run','--request',sys.argv[1],'--summary']))
"""
                command = [str(Path(NATIVE).resolve(strict=True)), "sandbox", "--permission-profile",
                    "nokiy_benchmark", "--include-managed-config", *permissions,
                    "-C", str(self.f.artifacts), "--", str(entry.with_name("python")),
                    "-I", "-B", "-c", code_body, str(request_path), json.dumps(native_settings), *denied]
            execute = run_process if native else caller._run_process
            code, _, failure, _ = execute(command, cwd=self.f.workspace,
                env=dict(os.environ) if native else child_env,
                stdin_path=source, stdout_path=self.f.root / "native-root.json",
                stderr_path=self.f.root / "native-root.stderr", timeout=40, output_limit=4 * 1024 * 1024)
            output = (self.f.root / "native-root.json").read_text()
            run = request.artifact_root / request.request_id
            diagnostics = {"requests": requests, "errors": errors,
                "launch_stderr": (self.f.root / "native-root.stderr").read_text()[-3000:],
                "stderr": (run / "core.stderr").read_text()[-3000:] if (run / "core.stderr").exists() else None,
                "events": (run / "core.jsonl").read_text()[-5000:] if (run / "core.jsonl").exists() else None}
            self.assertEqual(code, 0, json.dumps(diagnostics) + output[-2000:])
            self.assertIsNone(failure)
            self.assertEqual(len(fences), 0 if native else 1)
            terminal = json.loads(output)
            self.assertEqual(terminal["status"], "RESULT_AVAILABLE", terminal)
            self.assertEqual(terminal["result_inspection"]["status"], "EVIDENCE_VERIFIED", terminal)
            if malformed_hash:
                inspection = terminal["result_inspection"]
                self.assertEqual(inspection["source_read_rejection_reconciliation"], {
                    "schema_version": "nokiy_source_read_rejection_reconciliation_v1",
                    "not_attempted_count": 1, "unresolved_count": 0,
                    "effective_command_success": "known_success"})
                self.assertEqual(inspection["counts"]["failed"], 1)
                self.assertEqual(inspection["command_success"], "known_failure")
            self.assertEqual(terminal["result_inspection"]["cleanup"]["supervisor_pid"], "current_root_settled")
            self.assertTrue(terminal["cleanup_pass"], terminal)
            self.assertEqual(len(requests), expected_requests)
            self.assertEqual(errors, [])
            self.assertEqual({key: os.environ.get(key) for key in self.fixture.native_environment},
                             self.fixture.native_environment)
            self.assertEqual(json.loads(synthetic_auth.read_text()), {"tokens": {
                "access_token": handoff.DUMMY_API_KEY, "account_id": handoff.DUMMY_ACCOUNT_ID}})
            self.assertEqual(handoff.file_sha256(source), before)
            run = request.artifact_root / request.request_id
            receipt = caller.read_terminal(request.artifact_root, request.request_id)
            self.assertEqual(receipt["cleanup"]["isolation_source"], "native_command_root")
            events = [json.loads(line) for line in (run / "core.jsonl").read_text().splitlines()]
            if malformed_hash:
                rejected = [item for _, item in self.fixture._outputs(events)
                            if item.get("pre_execution_rejection", {}).get("rejection_kind")
                            == "invalid_expected_sha256"]
                self.assertEqual(len(rejected), 1)
                self.assertFalse(rejected[0]["pre_execution_rejection"]["source_content_read"])
                self.assertFalse(rejected[0]["pre_execution_rejection"]["process_started"])
            reads = [item for _, item in self.fixture._outputs(events) if item.get("path") == "answer.txt"]
            self.assertEqual(len(reads), 1)
            self.assertEqual(reads[0]["source_sha256"], before)
            self.assertIn("native root source evidence", reads[0]["stdout"])
            if delivery != "evidence_only":
                preview, truncated = handoff.core._text_preview(answer, request.max_result_bytes)
                self.assertEqual(receipt["result_text"], preview)
                if inline_answer:
                    self.assertEqual(terminal["result_text"], "")
                    self.assertEqual(terminal["result_text_omission"], "VERIFIED_PARENT_REVIEW_ANSWER")
                else:
                    self.assertEqual(terminal["result_text"], preview)
                self.assertEqual(terminal["result_truncated"], truncated)
                if long_answer:
                    artifact = run / "last-message.txt"
                    self.assertEqual(artifact.read_text(), answer)
                    self.assertEqual(json.loads(artifact.read_text()), json.loads(answer))
                    self.assertEqual(terminal["result_artifact"], {
                        "path": str(artifact), "bytes": len(answer.encode("utf-8")),
                        "sha256": handoff.file_sha256(artifact)})
                    if inline_answer:
                        projected = terminal["result_inspection"]["parent_review_answer"]
                        self.assertEqual(projected["text"], answer)
                        self.assertEqual(projected["artifact"], terminal["result_artifact"])
                        self.assertTrue(projected["parent_acceptance_required"])
                else:
                    self.assertEqual(json.loads(terminal["result_text"]), json.loads(answer))
            durable = {name: (run / name).read_bytes() for name in ("terminal.json", "core.jsonl")}
            self.assertEqual(caller.execute(request), receipt)
            self.assertEqual(len(requests), expected_requests,
                             "cached recovery must not call the provider again")
            for name, contents in durable.items():
                self.assertEqual((run / name).read_bytes(), contents)
            caller.verify_runtime_image(self.fixture.installed_identity, required_artifacts=handoff.core.REQUIRED)


if __name__ == "__main__":
    unittest.main()
