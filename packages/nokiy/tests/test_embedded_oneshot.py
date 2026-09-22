# SPDX-License-Identifier: MIT
"""Canonical one-shot behavior; independent of optional interactive Nokiy."""
from __future__ import annotations
import unittest
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import test_embedded_nokiy as fixtures
from codex_collaboration_harness.embedded_nokiy import EmbeddedNokiyError, execute, load_request, preflight, read_terminal


class OneShotCallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.EmbeddedNokiyFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_canonical_execution_has_no_gateway_or_session_database(self) -> None:
        request = load_request(self.fixture.request_path)
        ready = preflight(request)
        self.assertFalse(ready["ephemeral_gateway"])
        self.assertFalse(ready["ephemeral_session_db"])
        terminal = execute(request)
        self.assertEqual(terminal["status"], "RESULT_AVAILABLE", terminal)
        self.assertEqual(terminal["execution_model"], "single_task_native")
        run = request.artifact_root / request.request_id
        self.assertFalse((run / "gateway.log").exists())
        self.assertFalse((run / "ephemeral-state").exists())
        self.assertFalse((request.workspace / ".tura/session.db").exists())
        self.assertEqual(terminal["continuation_owner"], "codex")

    def test_existing_workspace_effect_evidence_is_not_session_garbage(self) -> None:
        existing = self.fixture.workspace / ".tura/run/keep-effect.json"
        existing.parent.mkdir(parents=True)
        original = b'{"state":"unsettled","do_not_replay":true}\n'
        existing.write_bytes(original)
        terminal = execute(load_request(self.fixture.request_path))
        self.assertEqual(terminal["status"], "RESULT_AVAILABLE", terminal)
        self.assertEqual(existing.read_bytes(), original)

    def test_only_explicit_next_request_dispatches_again(self) -> None:
        first = load_request(self.fixture.request_path)
        terminal = execute(first)
        self.assertEqual(execute(first), terminal)
        second = load_request(self.fixture._write_request(prompt="Continue from verified postimage"))
        self.assertNotEqual(second.request_id, first.request_id)
        next_terminal = execute(second)
        self.assertEqual(next_terminal["status"], "RESULT_AVAILABLE", next_terminal)
        self.assertNotEqual(next_terminal["execution_id"], terminal["execution_id"])
        self.assertEqual(next_terminal["continuation_owner"], "codex")
        self.assertFalse(next_terminal["session_persistence"])
        self.assertFalse(next_terminal["fallback_used"])
        runs = (self.fixture.runtime / "tura_router.runs").read_text().splitlines()
        self.assertEqual(runs, [first.request_id, second.request_id])

    def test_cached_terminal_requires_current_caller_binding(self) -> None:
        request = load_request(self.fixture.request_path)
        terminal = execute(request)
        os.environ["CODEX_THREAD_ID"] = "11111111-1111-1111-1111-111111111111"
        with self.assertRaisesRegex(EmbeddedNokiyError, "NOKIY_EMBEDDED_NATIVE_THREAD_MISMATCH"):
            execute(request)
        self.assertEqual(read_terminal(request.artifact_root, request.request_id), terminal)

    def test_caller_sigterm_settles_owned_router_and_returns_blocked_terminal(self) -> None:
        self.fixture._write_executable(self.fixture.runtime / "tura_router", f"#!{sys.executable}\n" +
            "import os,pathlib,time\npathlib.Path(__file__+'.pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
        self.fixture.runtime_image = self.fixture._write_runtime_image()
        request_path = self.fixture._write_request()
        request = load_request(request_path)
        caller = subprocess.Popen([sys.executable, "-B", "-m", "codex_collaboration_harness.embedded_nokiy",
                                   "run", "--request", str(request_path)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  start_new_session=True)
        router_pid = None
        try:
            marker = self.fixture.runtime / "tura_router.pid"
            deadline = time.monotonic() + 5
            while not marker.exists():
                self.assertIsNone(caller.poll(), "caller exited before fixture Router started")
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            router_pid = int(marker.read_text())
            caller.send_signal(signal.SIGTERM)
            stdout, stderr = caller.communicate(timeout=12)
            self.assertEqual(caller.returncode, 2, stderr)
            terminal = json.loads(stdout)
            self.assertEqual(terminal["first_typed_blocker"], "NOKIY_EMBEDDED_CANCELLED")
            self.assertEqual(read_terminal(request.artifact_root, request.request_id), terminal)
            with self.assertRaises(ProcessLookupError):
                os.kill(router_pid, 0)
        finally:
            if caller.poll() is None:
                caller.kill()
                caller.wait(timeout=5)
            if router_pid is not None:
                try:
                    os.killpg(router_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@unittest.skipUnless(os.environ.get("NOKIY_ONESHOT_TEST_BIN"), "explicit compiled candidate required")
class RealNativePipelineTests(unittest.TestCase):
    """Actual Router, Native worker, MCP and tools; only the provider is fake."""
    @classmethod
    def setUpClass(cls) -> None:
        binaries = Path(os.environ["NOKIY_ONESHOT_TEST_BIN"]).resolve(strict=True)
        cls.candidate = tempfile.TemporaryDirectory(prefix="oneshot-real-test-", dir=binaries.parent)
        cls.runtime = Path(cls.candidate.name)
        for name in ("tura_router", "tura_native_codex_worker", "tura_command_graph"):
            os.link(binaries / name, cls.runtime / name)
        prompt = Path(os.environ["NOKIY_ONESHOT_TEST_SOURCE"]) / "agents/src/balanced/prompt.md"
        (cls.runtime / "prompt.md").write_bytes(prompt.read_bytes())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.candidate.cleanup()

    def setUp(self) -> None:
        self.f = fixtures.EmbeddedNokiyFixture()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.runtime = self.runtime
        self.f.runtime_image = self.f._write_runtime_image()
        self.f._write_executable(self.f.codex, f"#!{sys.executable}\n" + r'''
import json,os,pathlib,subprocess,sys,time
if sys.argv[1:4] == ['mcp','list','--json']:
    print(json.dumps([{'name':'unrelated-mcp','enabled':True}]))
    sys.exit(0)
config={}
for i,arg in enumerate(sys.argv):
    if arg == '-c':
        key,value=sys.argv[i+1].split('=',1)
        config[key]=value
assert sys.argv[sys.argv.index('-s')+1] == 'read-only'
assert '--ephemeral' in sys.argv
assert '--ignore-user-config' not in sys.argv and '--ignore-rules' not in sys.argv
assert '-a' not in sys.argv and 'approval_policy' not in config
assert config['mcp_servers.unrelated-mcp.enabled'] == 'false'
assert config['mcp_servers.tura_command_graph.enabled'] == 'true'
for feature in ['apps','plugins','remote_plugin','skill_mcp_dependency_install']:
    assert any(sys.argv[i:i+2] == ['--disable',feature] for i in range(len(sys.argv)-1))
if 'model_provider' in config:
    assert json.loads(config['model_provider']) == 'fixture-provider'
wire_input=sys.stdin.read()
delta=json.loads(wire_input.split('[CURRENT_TASK_DELTA_V1]\n',1)[1])
mode=delta['instruction']
expected=('gpt-6-astra','high','default')
assert (sys.argv[sys.argv.index('-m')+1],json.loads(config['model_reasoning_effort']),json.loads(config['service_tier'])) == expected
with pathlib.Path(__file__+'.runs').open('a') as log: log.write(os.environ['NOKIY_NATIVE_EXECUTION_ID']+'\n')
pathlib.Path(__file__+'.home').write_text(os.environ['CODEX_HOME'])
def emit(value): print(json.dumps(value),flush=True)
emit({'type':'thread.started','thread_id':'synthetic-provider-thread'})
if mode in ['TIMEOUT','CANCEL']:
    child=subprocess.Popen(['/bin/sleep','60'])
    pathlib.Path(__file__+'.child').write_text(str(child.pid))
    time.sleep(60)
    sys.exit(1)
env_names=json.loads(config['mcp_servers.tura_command_graph.env_vars'])
assert 'TURA_COMMAND_RUN_SANDBOX' in env_names
env={key:os.environ[key] for key in env_names if key in os.environ}
for key in ['PATH','HOME','TMPDIR']:
    if key in os.environ: env[key]=os.environ[key]
exe=json.loads(config['mcp_servers.tura_command_graph.command'])
graph=subprocess.Popen([exe],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
def rpc(ident,method,params):
    graph.stdin.write(json.dumps({'jsonrpc':'2.0','id':ident,'method':method,'params':params})+'\n')
    graph.stdin.flush()
    row=json.loads(graph.stdout.readline())
    assert row.get('id') == ident and 'result' in row,row
    return row['result']
rpc(1,'initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'local-fixture','version':'1'}})
if mode in ['PATCH','FAIL_AFTER_EFFECT']:
    commands=[{'command':'apply_patch','command_line':'*** Begin Patch\n*** Add File: answer.txt\n+42\n*** End Patch'}]
elif mode == 'DENIED':
    commands=[{'command':'apply_patch','command_line':'*** Begin Patch\n*** Add File: denied.txt\n+must-not-exist\n*** End Patch'}]
else:
    commands=[{'command':'shell_command','command_line':json.dumps({'command':'cat answer.txt' if mode in ['CONTINUE','READ_PATCH_READ'] else 'cat input.txt','timeout_ms':2000})}]
arguments={'commands':commands}
if mode == 'PATCH': arguments['execution_id']='same-patch-effect'
result=rpc(2,'tools/call',{'name':'tura_command_graph','arguments':arguments})
if mode == 'READ_PATCH_READ':
    assert result['isError'] is False and 'before-edit-42' in json.dumps(result),result
    emit({'type':'item.completed','item':{'id':'read-before','type':'mcp_tool_call','server':'tura_command_graph','tool':'tura_command_graph','arguments':arguments,'result':result,'status':'completed'}})
    patch={'commands':[{'command':'apply_patch','command_line':'*** Begin Patch\n*** Update File: answer.txt\n@@\n-before-edit-42\n+after-edit-43\n*** End Patch'}]}
    edited=rpc(3,'tools/call',{'name':'tura_command_graph','arguments':patch})
    assert edited['isError'] is False,edited
    emit({'type':'item.completed','item':{'id':'patch-between','type':'mcp_tool_call','server':'tura_command_graph','tool':'tura_command_graph','arguments':patch,'result':edited,'status':'completed'}})
    result=rpc(4,'tools/call',{'name':'tura_command_graph','arguments':arguments})
    assert result['isError'] is False and 'after-edit-43' in json.dumps(result),result
if mode == 'PATCH':
    marker=pathlib.Path(os.environ['NOKIY_NATIVE_WORKSPACE'])/'answer.txt'
    before=(marker.read_bytes(),marker.stat().st_mtime_ns)
    replay=rpc(3,'tools/call',{'name':'tura_command_graph','arguments':arguments})
    # Existing Router policy refuses duplicate effect execution. It does not
    # promise identical RPC responses; terminal replay is owned by the caller.
    assert result['isError'] is False and replay['isError'] is True,(result,replay)
    assert before == (marker.read_bytes(),marker.stat().st_mtime_ns)
    emit({'type':'item.completed','item':{'id':'mcp-duplicate','type':'mcp_tool_call','server':'tura_command_graph','tool':'tura_command_graph','arguments':arguments,'result':replay,'status':'completed'}})
pathlib.Path(__file__+'.tools.json').write_text(json.dumps(result))
graph.stdin.close()
assert graph.wait(timeout=5) == 0,graph.stderr.read()
emit({'type':'item.completed','item':{'id':'mcp-effect-1','type':'mcp_tool_call','server':'tura_command_graph','tool':'tura_command_graph','arguments':arguments,'result':result,'status':'completed'}})
if mode == 'FAIL_AFTER_EFFECT': emit({'type':'turn.failed','error':{'message':'fixture failure after real effect'}})
emit({'type':'item.completed','item':{'id':'final-1','type':'agent_message','text':json.dumps(result)}})
emit({'type':'turn.completed','usage':{'input_tokens':120,'output_tokens':7,'cached_input_tokens':80}})
''')
        (self.f.workspace / "input.txt").write_text("input-evidence-42\n")

    def request(self, mode: str, *, write: bool = False):
        self.f.context, self.f.jspace = self.f._write_context(write_scopes=["answer.txt"] if write else [])
        path = self.f._write_request(prompt=mode)
        value = json.loads(path.read_text())
        value["authority_effect"] = "workspace" if write else "none"
        value["max_trajectory_bytes"] = 1024 * 1024
        path.write_text(json.dumps(value))
        return load_request(path)

    def check_result(self, request, expected: str = "RESULT_AVAILABLE"):
        result = execute(request)
        diagnostic = Path(result["stderr_artifact"]["path"]).read_text() if result.get("stderr_artifact") else ""
        self.assertEqual(result["status"], expected, (result, diagnostic))
        self.assertFalse((self.f.workspace / ".tura/session.db").exists())
        self.assertFalse((request.artifact_root / request.request_id / "gateway.log").exists())
        return result

    def test_real_read_tool_and_single_terminal(self) -> None:
        result = self.check_result(self.request("READ"))
        self.assertEqual(result["tool_loop"]["successful_count"], 1)
        self.assertIn("input-evidence-42", Path(result["result_artifact"]["path"]).read_text())
        self.assertTrue(result["cleanup_pass"])
        self.assertEqual(Path(Path(str(self.f.codex)+".home").read_text()), self.f.codex_home)
        self.assertTrue(self.f.codex_home.exists())

    def test_real_same_read_after_edit_is_not_a_duplicate_effect(self) -> None:
        (self.f.workspace / "answer.txt").write_text("before-edit-42\n")
        result = self.check_result(self.request("READ_PATCH_READ", write=True))
        self.assertEqual(result["tool_loop"]["successful_count"], 3)
        self.assertIn("after-edit-43", Path(result["result_artifact"]["path"]).read_text())

    def test_real_default_astra_profile_reaches_native_without_tier_downgrade(self) -> None:
        self.request("DEFAULT_PROFILE")
        path = self.f._write_request(prompt="DEFAULT_PROFILE")
        value = json.loads(path.read_text())
        for key in ("model", "reasoning_effort", "model_acceleration"):
            value.pop(key)
        path.write_text(json.dumps(value))
        result = self.check_result(load_request(path))
        self.assertEqual(result["model_route"], "native_codex/gpt-6-astra")
        self.assertEqual(result["reasoning_effort"], "high")
        self.assertEqual(result["requested_service_tier"], "default")
        self.assertIsNone(result["observed_service_tier"])
        self.assertEqual(result["tool_loop"]["successful_count"], 1)

    def test_real_explicit_provider_is_bound_without_auth_copy(self) -> None:
        self.request("READ")
        path = self.f._write_request(prompt="READ")
        value = json.loads(path.read_text())
        value["model_provider"] = "fixture-provider"
        path.write_text(json.dumps(value))
        (self.f.codex_home / "auth.json").unlink()
        result = self.check_result(load_request(path))
        self.assertEqual(result["requested_model_provider"], "fixture-provider")
        self.assertIsNone(result["observed_model_provider"])
        self.assertEqual(Path(Path(str(self.f.codex)+".home").read_text()), self.f.codex_home)

    def test_real_caller_cancel_reaps_provider_descendant_and_returns_terminal(self) -> None:
        self.request("CANCEL")
        path = self.f._write_request(prompt="CANCEL")
        value = json.loads(path.read_text())
        value["timeout_seconds"] = 60
        path.write_text(json.dumps(value))
        request = load_request(path)
        caller = subprocess.Popen([sys.executable, "-B", "-m", "codex_collaboration_harness.embedded_nokiy",
                                   "run", "--request", str(path)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True)
        child_pid = None
        try:
            marker = Path(str(self.f.codex) + ".child")
            deadline = time.monotonic() + 10
            while not marker.exists():
                self.assertIsNone(caller.poll(), "caller exited before fixture provider started")
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            child_pid = int(marker.read_text())
            caller.send_signal(signal.SIGTERM)
            stdout, stderr = caller.communicate(timeout=45)
            self.assertEqual(caller.returncode, 2, stderr)
            terminal = json.loads(stdout)
            self.assertEqual(terminal["first_typed_blocker"], "NOKIY_EMBEDDED_CANCELLED")
            self.assertTrue(terminal["cleanup"]["process_stopped"], terminal)
            self.assertEqual(terminal["cleanup"]["unsettled_descendant_pids"], [])
            # Cancellation does not fabricate the worker's missing terminal proof.
            self.assertFalse(terminal["cleanup_pass"])
            self.assertEqual(read_terminal(request.artifact_root, request.request_id), terminal)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if caller.poll() is None:
                caller.kill()
                caller.wait(timeout=5)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_real_patch_replay_then_parent_next_execution(self) -> None:
        first = self.request("PATCH", write=True)
        result = self.check_result(first)
        self.assertEqual((self.f.workspace / "answer.txt").read_text(), "42\n")
        self.assertEqual(execute(first), result)
        second = self.request("CONTINUE", write=True)
        self.assertNotEqual(first.request_id, second.request_id)
        next_result = self.check_result(second)
        self.assertIn("42", Path(next_result["result_artifact"]["path"]).read_text())
        self.assertEqual(Path(str(self.f.codex)+".runs").read_text().splitlines(), [first.request_id, second.request_id])
        self.assertTrue(any((self.f.workspace / ".tura/run").rglob("*.json")), "effect receipts must survive")

    def test_real_out_of_scope_write_never_happens(self) -> None:
        result = self.check_result(self.request("DENIED", write=True), "BLOCKED")
        self.assertFalse((self.f.workspace / "denied.txt").exists())
        self.assertEqual(result["tool_loop"]["successful_count"], 0)
        self.assertTrue(result["cleanup_pass"])

    def test_real_failed_execution_preserves_effect_and_never_retries(self) -> None:
        request = self.request("FAIL_AFTER_EFFECT", write=True)
        result = self.check_result(request, "BLOCKED")
        self.assertEqual((self.f.workspace / "answer.txt").read_text(), "42\n")
        self.assertEqual(execute(request), result)
        self.assertEqual(len(Path(str(self.f.codex)+".runs").read_text().splitlines()), 1)

    def test_real_binding_tamper_rejected_before_provider(self) -> None:
        from codex_collaboration_harness.embedded_nokiy import _prepare
        ready, image, wire = _prepare(self.request("READ"))
        wire["provider_profile"]["reasoning_effort"] = "low"
        process = subprocess.run([str(image.artifacts["tura_router"].path),"native-once","--worker",
                                 str(image.artifacts["tura_native_codex_worker"].path),"--worker-sha256",
                                 image.artifacts["tura_native_codex_worker"].sha256],
                                input=json.dumps(wire),capture_output=True,text=True,timeout=15)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("EXECUTION_BINDING_MISMATCH", process.stderr)
        self.assertFalse(Path(str(self.f.codex)+".runs").exists())

    def test_real_deadline_reaps_provider_descendant(self) -> None:
        result = self.check_result(self.request("TIMEOUT"), "BLOCKED")
        pid = int(Path(str(self.f.codex)+".child").read_text())
        deadline = time.monotonic()+3
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            self.assertLess(time.monotonic(), deadline, "owned provider descendant is still alive")
            time.sleep(0.02)
        self.assertFalse(result["fallback_used"])


if __name__ == "__main__":
    unittest.main()
