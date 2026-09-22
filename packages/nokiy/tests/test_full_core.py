# SPDX-License-Identifier: MIT
"""Real caller/service processes with synthetic core binaries; no provider send."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

import test_embedded_nokiy as fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core as core


class FullCoreTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EmbeddedNokiyFixture()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.runtime = self.f.root / "full-image"
        self.f.runtime.mkdir()
        for name, relative in core.SOURCE_PATHS.items():
            path = self.f.runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}" if name.endswith("config") else "Fixture full-core prompt")
        service = f"#!{sys.executable}\n" + r'''
import fcntl,json,os,pathlib,sys,time
name=pathlib.Path(sys.argv[0]).name
root=pathlib.Path(os.environ['TURA_HOME'])/'session_log';root.mkdir(exist_ok=True)
marker='router.addr' if name=='tura_router' else 'service.addr'
if name=='tura_session_db':
    locks=root.parent/'.tura/locks';locks.mkdir(parents=True)
    owner=(locks/'session-db-debug.lock').open('w+')
    fcntl.flock(owner,fcntl.LOCK_EX)
    owner.write('pid='+str(os.getpid())+'\nkind=session_db\n');owner.flush()
    endpoint={'addr':'127.0.0.1:23456','version':'test'}
else: endpoint={'pid':os.getpid(),'addr':'127.0.0.1:23456'}
(root/marker).write_text(json.dumps(endpoint))
time.sleep(120)
'''
        for name in ("tura_session_db", "tura_router", "tura_runtime"):
            self.f._write_executable(self.f.runtime / name, service)
        self.f._write_executable(self.f.runtime / "tura_exec", f"#!{sys.executable}\n" + r'''
import json,os,pathlib,subprocess,sys,time
args=sys.argv[1:]
def value(key): return args[args.index(key)+1]
assert '--sandbox' in args and '--embedded' not in args
assert value('--router-address')==os.environ['TURA_ROUTER_ADDR']
assert value('-m')=='codex/gpt-6-astra'
assert os.environ['TURA_GATEWAY_CALLBACKS']=='0'
capsule=json.loads(pathlib.Path(value('--task-context-capsule')).read_text())
jspace=json.loads(pathlib.Path(value('--jspace-contract')).read_text())
assert capsule['jspace_semantic_sha256']==jspace['semantic_sha256']
state=pathlib.Path(os.environ['TURA_HOME'])
(state/'cli-reached').write_text(str(os.getpid()))
prompt=sys.stdin.read()
if prompt=='TIMEOUT':
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=True)
    (state/'grandchild.pid').write_text(str(child.pid))
    time.sleep(120)
if prompt=='FAIL': sys.exit(2)
if prompt=='FAIL_AFTER_EDIT':
    pathlib.Path(value('-C'),'answer.txt').write_text('effect before failure\n')
    sys.exit(2)
if prompt=='STATUS_ONLY':
    print(json.dumps({'type':'item.completed','item':{'type':'command_execution','status':'completed',
        'aggregated_output':json.dumps({'task_status':{'task_group':'fixture'}})}}))
else:
    pathlib.Path(value('-C'),'answer.txt').write_text('full-core result\n')
    print(json.dumps({'type':'item.completed','item':{'type':'file_change','status':'completed'}}))
print(json.dumps({'type':'item.completed','item':{'type':'assistant_message','text':'finished edit and verification'}}))
print(json.dumps({'type':'turn.completed','status':'completed','usage':{'total_tokens':11},
    'model':value('-m'),'agent':value('-a'),'session_id':value('--session-id'),'cwd':value('-C'),
    'reasoning_effort':value('--model-reasoning-effort'),'service_tier':value('--service-tier')}))
''')
        self._image()
        self.f.context, self.f.jspace = self.f._write_context(write_scopes=["answer.txt"])
        self._request()

    def _image(self):
        paths = {name: self.f.runtime / name for name in core.REQUIRED - core.SOURCE_PATHS.keys()}
        paths.update({name: self.f.runtime / relative for name, relative in core.SOURCE_PATHS.items()})
        self.f.runtime_image = self.f.root / "full-image.json"
        self.f.runtime_image.write_text(json.dumps({
            "schema_version":"tura-dcf-benchmark-frozen-full-runtime-image/v1",
            "status":"FROZEN_BYTE_EXACT", "runtime_build_identity":"synthetic-full-core",
            "runtime_root":str(self.f.runtime), "artifacts":{
                name:{"path":str(path), "sha256":fixtures.file_sha256(path), "size":path.stat().st_size}
                for name,path in paths.items()}}))

    def _request(self, profile="direct", prompt="edit and verify"):
        self.f.request_path = self.f._write_request(prompt=prompt)
        value = json.loads(self.f.request_path.read_text())
        value.update(execution_profile=profile, authority_effect="workspace")
        self.f.request_path.write_text(json.dumps(value))
        return caller.load_request(self.f.request_path)

    def test_both_profiles_are_bound_and_do_not_change_native_request_defaults(self):
        direct = self._request()
        balanced = self._request("balanced")
        self.assertNotEqual(direct.request_id, balanced.request_id)
        self.assertEqual(caller.preflight(balanced)["execution_profile"], "balanced")
        value = direct.to_wire(include_identity=False)
        value.pop("execution_profile")
        self.assertIsNone(caller.decode_request(value).execution_profile)
        for profile in ("native", "fast", None, [], {}):
            value["execution_profile"] = profile
            with self.assertRaises(caller.EmbeddedNokiyError):
                caller.decode_request(value)

    def test_nokiy_name_omits_backend_branding_and_preserves_request_identity(self):
        for profile in ("direct", "balanced"):
            request = self._request(profile)
            before = request.to_wire()
            ready = caller.preflight(request)
            self.assertEqual(ready["executor_name"], "nokiy")
            self.assertNotIn("execution_backend", ready)
            self.assertEqual(ready["execution_profile"], profile)
            self.assertEqual(request.to_wire(), before)
            self.assertNotIn("executor_name", before)
            self.assertEqual(
                caller.decode_request(request.to_wire(include_identity=False)).request_id,
                request.request_id,
            )

    def test_context_and_image_drift_fail_before_execution(self):
        request = self._request()
        self.f.context.write_text("{}")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "CONTEXT_DRIFT"):
            caller.preflight(request)
        self.assertFalse((request.artifact_root / request.request_id).exists())

    def _tool_events(self, observations):
        request = self._request()
        path = self.f.root / "observations.jsonl"
        events = [{"type": "item.completed", "item": item} for item in observations]
        events.append({"type": "turn.completed", "status": "completed",
                       "model": "codex/" + request.model, "agent": request.execution_profile,
                       "session_id": "full-" + request.request_sha256, "cwd": str(request.workspace),
                       "reasoning_effort": request.reasoning_effort, "service_tier": "default",
                       "usage": {"total_tokens": 11}})
        path.write_text("\n".join(json.dumps(event) for event in events))
        return core._events(path, request.max_trajectory_bytes, request)

    def test_bookkeeping_never_satisfies_required_execution_observation(self):
        for representation in ({"command_type": "task_status"}, {"command": "task_status"},
                               {"aggregated_output": json.dumps({"task_status": {"task_group": "test"}})}):
            with self.subTest(representation=representation):
                item = {"type": "command_execution", "status": "completed", **representation}
                result = self._tool_events([item])
                self.assertEqual(result["tool_loop"], {"completed_count": 0, "successful_count": 0})
                self.assertEqual(result["usage"], {"total_tokens": 11})

    def test_nested_command_failure_is_not_success_despite_completed_wrapper(self):
        for result in ({"exit_code": 1}, {"exit_code": False}, {"success": False}, {"isError": True}):
            with self.subTest(result=result):
                item = {"type": "command_execution", "status": "completed", "exit_code": None,
                        "aggregated_output": json.dumps(result)}
                self.assertEqual(self._tool_events([item])["tool_loop"],
                                 {"completed_count": 1, "successful_count": 0})

    def test_real_commands_and_file_changes_still_count_beside_bookkeeping(self):
        items = [{"type": "command_execution", "status": "completed", "command_type": "task_status"},
                 {"type": "command_execution", "status": "completed", "exit_code": None,
                  "aggregated_output": json.dumps({"exit_code": 0, "stdout": "ok"})},
                 {"type": "file_change", "status": "completed"}]
        self.assertEqual(self._tool_events(items)["tool_loop"],
                         {"completed_count": 2, "successful_count": 2})

    def test_unlisted_image_file_is_not_trusted(self):
        request = self._request()
        (self.f.runtime / "unlisted-plugin.json").write_text("{}")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "INVENTORY_MISMATCH"):
            caller.preflight(request)

    def test_environment_preserves_native_identity_and_clears_foreign_nokiy_routes(self):
        request = self._request()
        _, runtime, _, _ = core.prepare(request)
        before = os.environ.get("TURA_ROUTER_ADDR")
        os.environ["TURA_ROUTER_ADDR"] = "127.0.0.1:1"
        try:
            env = core._environment(request, runtime, self.f.root / "state")
        finally:
            if before is None: os.environ.pop("TURA_ROUTER_ADDR")
            else: os.environ["TURA_ROUTER_ADDR"] = before
        self.assertNotIn("TURA_ROUTER_ADDR", env)
        self.assertEqual(env["CODEX_HOME"], os.environ["CODEX_HOME"])
        self.assertEqual(env.get("HOME"), os.environ.get("HOME"))

    def test_uncertain_request_is_not_reexecuted(self):
        request = self._request()
        (request.artifact_root / request.request_id).mkdir()
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "UNCERTAIN_PRIOR_ATTEMPT"):
            caller.execute(request)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_full_caller_edits_and_replays_without_new_execution(self):
        request = self._request()
        result = caller.execute(request)
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)
        self.assertEqual((self.f.workspace / "answer.txt").read_text(), "full-core result\n")
        self.assertTrue(result["cleanup_pass"], result)
        self.assertEqual(result["usage"], {"total_tokens":11})
        self.assertEqual(result["continuation_owner"], "codex")
        self.assertEqual(result["executor_name"], "nokiy")
        self.assertNotIn("execution_backend", result)
        self.assertEqual(caller.execute(request), result)
        self.assertFalse(result["fallback_used"])

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_metadata_only_provider_output_cannot_complete_required_tool_request(self):
        request = self._request(prompt="STATUS_ONLY")
        result = caller.execute(request)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(result["first_typed_blocker"], "NOKIY_EMBEDDED_TOOL_LOOP_NOT_OBSERVED")
        self.assertEqual(result["tool_loop"], {"completed_count": 0, "successful_count": 0})
        self.assertTrue(result["cleanup_pass"])
        self.assertFalse((self.f.workspace / "answer.txt").exists())

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_timeout_settles_child_that_created_its_own_session(self):
        request = self._request(prompt="TIMEOUT")
        result = caller.execute(request)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertTrue(result["cleanup_pass"], result)
        pid = int((request.artifact_root / request.request_id / "execution-state/grandchild.pid").read_text())
        info = core.supervise.__globals__["DarwinProcesses"]().info(pid)
        self.assertTrue(info is None or info.status == 5)
        self.assertEqual(caller.execute(request), result)

    def _cancel_inflight(self, profile, cancel_signal):
        self._request(profile, prompt="TIMEOUT")
        value = json.loads(self.f.request_path.read_text())
        value["timeout_seconds"] = 60
        self.f.request_path.write_text(json.dumps(value))
        request = caller.load_request(self.f.request_path)
        run = request.artifact_root / request.request_id
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "codex_collaboration_harness.embedded_nokiy",
             "run", "--request", str(self.f.request_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            marker = run / "execution-state/grandchild.pid"
            deadline = time.monotonic() + 10
            while not marker.exists():
                self.assertIsNone(process.poll(), "caller exited before descendant started")
                self.assertLess(time.monotonic(), deadline, "fixture did not start")
                time.sleep(.02)
            descendant_pid = int(marker.read_text())
            cli_marker = run / "execution-state/cli-reached"
            cli_identity = cli_marker.read_bytes()
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "UNCERTAIN_PRIOR_ATTEMPT"):
                caller.execute(request)
            self.assertIsNone(process.poll(), "duplicate request interrupted its owner")
            self.assertEqual(cli_marker.read_bytes(), cli_identity)
            self.assertFalse((run / "terminal.json").exists())

            process.send_signal(cancel_signal)
            stdout, stderr = process.communicate(timeout=45)
            self.assertEqual(process.returncode, 2, stderr)
            terminal = json.loads(stdout)
            self.assertEqual(terminal["first_typed_blocker"], "NOKIY_EMBEDDED_CANCELLED")
            self.assertEqual(terminal["status"], "BLOCKED")
            self.assertEqual(terminal["execution_profile"], profile)
            self.assertEqual(terminal["continuation_owner"], "codex")
            self.assertTrue(terminal["cleanup_pass"], terminal)
            info = core.supervise.__globals__["DarwinProcesses"]().info(descendant_pid)
            self.assertTrue(info is None or info.status == 5, info)
            self.assertFalse((self.f.workspace / "answer.txt").exists())
            before = {str(path.relative_to(run)): fixtures.file_sha256(path)
                      for path in run.rglob("*") if path.is_file()}
            self.assertEqual(caller.execute(request), terminal)
            after = {str(path.relative_to(run)): fixtures.file_sha256(path)
                     for path in run.rglob("*") if path.is_file()}
            self.assertEqual(before, after, "cancelled execution was replayed or altered")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=45)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_parent_sigterm_and_inflight_duplicate_for_both_profiles(self):
        for profile in ("direct", "balanced"):
            with self.subTest(profile=profile):
                self._cancel_inflight(profile, signal.SIGTERM)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_parent_sigint_and_inflight_duplicate_for_both_profiles(self):
        for profile in ("direct", "balanced"):
            with self.subTest(profile=profile):
                self._cancel_inflight(profile, signal.SIGINT)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_failure_after_effect_is_preserved_and_never_replayed(self):
        for profile in ("direct", "balanced"):
            with self.subTest(profile=profile):
                request = self._request(profile, prompt="FAIL_AFTER_EDIT")
                result = caller.execute(request)
                self.assertEqual(result["status"], "BLOCKED", result)
                self.assertEqual(result["first_typed_blocker"], "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED")
                self.assertTrue(result["cleanup_pass"], result)
                answer = self.f.workspace / "answer.txt"
                self.assertEqual(answer.read_text(), "effect before failure\n")
                answer.write_text("parent reconciled effect\n")
                self.assertEqual(caller.execute(request), result)
                self.assertEqual(answer.read_text(), "parent reconciled effect\n")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_session_db_startup_failure_is_terminal_and_clean(self):
        self.f._write_executable(self.f.runtime / "tura_session_db", f"#!{sys.executable}\n" + r'''
import os,pathlib,sys
(pathlib.Path(os.environ['TURA_HOME'])/'session-db-started').write_text(str(os.getpid()))
sys.exit(23)
''')
        self._image()
        for profile in ("direct", "balanced"):
            with self.subTest(profile=profile):
                request = self._request(profile)
                state = request.artifact_root / request.request_id / "execution-state"
                answer = self.f.workspace / "answer.txt"
                answer.write_text("parent-owned answer\n")
                before = (answer.read_bytes(), answer.stat().st_mtime_ns)
                result = caller.execute(request)
                self.assertEqual(result["status"], "BLOCKED", result)
                self.assertEqual(result["first_typed_blocker"], "NOKIY_FULL_CORE_SERVICE_NOT_READY")
                self.assertIs(result["cleanup_pass"], True, result)
                self.assertEqual(result["execution_profile"], profile)
                self.assertTrue((state / "session-db-started").is_file())
                self.assertFalse((state / "session_log/service.addr").exists())
                self.assertFalse((state / "cli-reached").exists())
                self.assertEqual((answer.read_bytes(), answer.stat().st_mtime_ns), before)
                self.assertEqual(caller.execute(request), result)
                self.assertFalse((state / "cli-reached").exists())
                self.assertEqual((answer.read_bytes(), answer.stat().st_mtime_ns), before)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_router_startup_failure_is_terminal_and_clean(self):
        self.f._write_executable(self.f.runtime / "tura_router", f"#!{sys.executable}\n" + r'''
import os,pathlib,sys
(pathlib.Path(os.environ['TURA_HOME'])/'router-started').write_text(str(os.getpid()))
sys.exit(23)
''')
        self._image()
        for profile in ("direct", "balanced"):
            with self.subTest(profile=profile):
                request = self._request(profile)
                state = request.artifact_root / request.request_id / "execution-state"
                answer = self.f.workspace / "answer.txt"
                answer.write_text("parent-owned answer\n")
                before = (answer.read_bytes(), answer.stat().st_mtime_ns)
                result = caller.execute(request)
                self.assertEqual(result["status"], "BLOCKED", result)
                self.assertEqual(result["first_typed_blocker"], "NOKIY_FULL_CORE_SERVICE_NOT_READY")
                self.assertIs(result["cleanup_pass"], True, result)
                self.assertEqual(result["execution_profile"], profile)
                self.assertTrue((state / "router-started").is_file())
                self.assertTrue((state / "session_log/service.addr").is_file())
                self.assertFalse((state / "session_log/router.addr").exists())
                owner = dict(line.split("=", 1) for line in
                             (state / ".tura/locks/session-db-debug.lock").read_text().splitlines())
                self.assertEqual(owner["kind"], "session_db")
                pid = int(owner["pid"])
                self.assertGreater(pid, 0)
                info = core.supervise.__globals__["DarwinProcesses"]().info(pid)
                self.assertTrue(info is None or info.status == 5, info)
                self.assertFalse((state / "cli-reached").exists())
                self.assertEqual((answer.read_bytes(), answer.stat().st_mtime_ns), before)
                self.assertEqual(caller.execute(request), result)
                self.assertFalse((state / "cli-reached").exists())
                self.assertEqual((answer.read_bytes(), answer.stat().st_mtime_ns), before)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_provider_failure_remains_terminal_not_retry(self):
        request = self._request(prompt="FAIL")
        result = caller.execute(request)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(result["first_typed_blocker"], "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED")
        self.assertEqual(result["executor_name"], "nokiy")
        self.assertNotIn("execution_backend", result)
        self.assertTrue(result["cleanup_pass"], result)
        self.assertEqual(caller.execute(request), result)


if __name__ == "__main__":
    unittest.main()
