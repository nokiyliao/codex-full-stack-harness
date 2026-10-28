"""Native batch process ownership, without models or production effects."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import hashlib
from http.server import ThreadingHTTPServer
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
from unittest.mock import patch

from codex_collaboration_harness import graph_process as graph


NATIVE = os.environ.get("NOKIY_NATIVE_SANDBOX_TEST_BIN")
SOURCE = str(Path(graph.__file__).resolve().parents[1])
PROGRAM = f"import sys;sys.path.insert(0,{SOURCE!r})\n" + r'''
import dataclasses,json,os,signal,threading,time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from codex_collaboration_harness import graph_process as graph
spec=json.loads(sys.argv[1])
cancelled=threading.Event()
for signum in (signal.SIGINT,signal.SIGTERM):
    signal.signal(signum,lambda *_:cancelled.set())
Path('root.pid').write_text(str(os.getpid()))
with graph.native_batch_scope([str(i) for i in range(len(spec['bodies']))],spec['parallel']) as scope:
    assert scope is not None
    def worker(index,body):
        try:
            with scope.member(str(index)):
                root=graph.inherited_root_parent()
                deadline=time.monotonic()+3
                for marker in spec.get('after_markers',{}).get(str(index),[]):
                    while not Path(marker).exists():
                        if cancelled.is_set(): raise RuntimeError('GRAPH_BATCH_CANCELLED_BEFORE_SPAWN')
                        if time.monotonic()>deadline: raise RuntimeError('fixture start ordering failed')
                        time.sleep(.005)
                argv=[sys.executable,'-I','-B','-c',body] if body is not None else ['/nonexistent/nokiy-fixture']
                result=graph.supervise(argv,cwd=os.getcwd(),env=dict(os.environ),input_bytes=b'',
                    timeout=4,cancelled=cancelled,max_stdout=4096,max_stderr=4096,parent_pid=root,
                    terminate_grace=.1)
                return dataclasses.asdict(result)
        except Exception as error:
            return {'error':str(error)}
    with ThreadPoolExecutor(max_workers=len(spec['bodies'])) as pool:
        futures=[pool.submit(copy_context().run,worker,i,body) for i,body in enumerate(spec['bodies'])]
        results=[future.result() for future in futures]
print(json.dumps(results,default=lambda value:value.decode()))
'''


class BatchScopeContractTests(unittest.TestCase):
    def scope(self, keys=("a", "b"), parallel=2):
        self.enterContext(patch.object(graph, "inherited_root_parent", return_value=os.getppid()))
        processes = self.enterContext(patch.object(graph, "DarwinProcesses"))
        processes.return_value.members.return_value = {}
        scope = self.enterContext(graph.native_batch_scope(list(keys), parallel))
        return scope, processes.return_value

    def test_unknown_duplicate_and_repeated_claims_are_not_grants(self):
        scope, processes = self.scope()
        with self.assertRaisesRegex(graph.ProcessScopeError, "MEMBER_UNKNOWN"):
            with scope.member("foreign"):
                pass
        for key in ("a", "b"):
            with scope.member(key):
                member = graph._batch_member.get()
                self.assertEqual(scope.claim(member), os.getppid())
                with self.assertRaisesRegex(graph.ProcessScopeError, "ROOT_CLAIM_INVALID"):
                    scope.claim(member)
                with self.assertRaisesRegex(graph.ProcessScopeError, "ALREADY_CONSUMED"):
                    with scope.member(key):
                        pass
                with patch.object(graph.os, "getpid", return_value=scope.pid + 1):
                    with self.assertRaisesRegex(graph.ProcessScopeError, "IDENTITY_CHANGED"):
                        scope._check(member)
        processes.members.assert_called_once()
        self.assertIsNone(graph._batch_member.get())

    def test_failure_before_supervision_does_not_strand_a_peer(self):
        scope, processes = self.scope()
        started = threading.Event()
        def finished():
            with scope.member("a"):
                member = graph._batch_member.get()
                scope.claim(member)
                started.set()
                return scope.finish(member, wait=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(copy_context().run, finished)
            self.assertTrue(started.wait(2))
            with self.assertRaisesRegex(RuntimeError, "failed before spawn"):
                with scope.member("b"):
                    raise RuntimeError("failed before spawn")
            self.assertTrue(future.result(timeout=2)[1])
        self.assertTrue(scope.cancelled.is_set())
        processes.members.assert_called_once()

    def test_originating_engine_failure_is_not_relabelled_as_peer_failure(self):
        scope, processes = self.scope()
        ready = threading.Barrier(2)
        def run(key, program):
            with scope.member(key):
                parent = scope.claim(graph._batch_member.get())
                ready.wait(timeout=2)
                return graph.supervise(
                    [sys.executable, "-I", "-c", program], cwd=os.getcwd(), env=dict(os.environ),
                    input_bytes=b"", timeout=3, cancelled=threading.Event(),
                    max_stdout=4096, max_stderr=4096, parent_pid=parent, terminate_grace=.1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            origin = pool.submit(copy_context().run, run, "a",
                                 'import sys,time;time.sleep(.15);print("provider failed");sys.exit(2)')
            peer = pool.submit(copy_context().run, run, "b", 'import time;time.sleep(2)')
            first, cancelled = origin.result(timeout=5), peer.result(timeout=5)
        self.assertEqual(first.returncode, 2)
        self.assertIsNone(first.failure, "the engine result must retain its own failure")
        self.assertIn(b"provider failed", first.stdout)
        self.assertEqual(cancelled.failure, "GRAPH_BATCH_PEER_FAILED")
        self.assertTrue(first.scope["no_live_descendants"])
        self.assertTrue(cancelled.scope["no_live_descendants"])
        processes.members.assert_called_once()

    def test_aborted_unsubmitted_member_cannot_later_start(self):
        scope, _ = self.scope()
        with scope.member("a"):
            scope.abort()
            with self.assertRaisesRegex(graph.ProcessScopeError, "ALREADY_CONSUMED"):
                with scope.member("b"):
                    pass
        self.assertTrue(scope.result[1])

    def test_unsandboxed_batch_uses_existing_member_fences(self):
        with patch.object(graph, "inherited_root_parent", return_value=None), \
                patch.object(graph, "DarwinProcesses") as processes:
            with graph.native_batch_scope(["a"], 1) as scope:
                self.assertIsNone(scope)
            processes.assert_not_called()

    def test_member_acquires_once_before_preparation_and_supervision_reuses_slot(self):
        scope, _ = self.scope(parallel=1)
        for key in ("a", "b"):
            with scope.member(key, cancelled=lambda: False):
                member = graph._batch_member.get()
                self.assertTrue(member.slot, "execution budget must start after admission")
                self.assertFalse(scope.slots.acquire(blocking=False))
                scope.claim(member)
                scope.enter(member, threading.Event())
            self.assertFalse(member.slot)
        self.assertTrue(scope.result[1])

    def test_cancelled_queued_member_never_prepares_and_does_not_leak_slot(self):
        scope, _ = self.scope(parallel=1)
        cancelled = threading.Event()
        queued = threading.Event()

        def prepare():
            queued.set()
            with scope.member("b", cancelled=cancelled.is_set):
                self.fail("cancelled queued member entered preparation")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with scope.member("a", cancelled=lambda: False):
                future = pool.submit(copy_context().run, prepare)
                self.assertTrue(queued.wait(2))
                cancelled.set()
                with self.assertRaisesRegex(graph.ProcessScopeError, "CANCELLED_BEFORE_SPAWN"):
                    future.result(timeout=2)
        self.assertTrue(scope.slots.acquire(blocking=False))
        scope.slots.release()
        self.assertTrue(scope.result[1])


@unittest.skipUnless(sys.platform == "darwin" and NATIVE, "explicit native sandbox fixture")
class NativeBatchProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-batch-", dir=os.environ.get("NOKIY_GRAPH_TEST_ROOT"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.prefix = [str(Path(NATIVE).resolve(strict=True)), "sandbox", "--permission-profile",
            "nokiy_benchmark", "--include-managed-config", "-c", 'default_permissions="nokiy_benchmark"',
            "-c", 'permissions.nokiy_benchmark.extends=":workspace"',
            "-c", "permissions.nokiy_benchmark.network.enabled=true", "--"]

    @staticmethod
    def settle(process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)

    def launch(self, bodies, parallel, after_markers=None):
        process = subprocess.Popen([*self.prefix, sys.executable, "-I", "-B", "-c", PROGRAM,
            json.dumps({"bodies": bodies, "parallel": parallel, "after_markers": after_markers or {}})], cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.settle, process)
        return process

    def result(self, process):
        stdout, stderr = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 0, stderr + stdout)
        return json.loads(stdout)

    @staticmethod
    def timed_body(delay, marker):
        return ("import json,os,time\nfrom pathlib import Path\nstart=time.monotonic()\n"
                f"Path({marker!r}).write_text(str(os.getpid()))\ntime.sleep({delay})\n"
                "print(json.dumps({'start':start,'end':time.monotonic(),'pid':os.getpid()}))\n")

    def assert_clean(self, row):
        self.assertTrue(row["scope"]["engine_reaped"], row)
        self.assertTrue(row["scope"]["no_live_descendants"], row)
        self.assertIsNone(row["scope"]["cleanup_error"], row)
        self.assertEqual(row["scope"]["cleanup_scope"], "whole_native_batch")

    def test_three_members_overlap_and_reuse_two_slots_without_killing_peers(self):
        results = self.result(self.launch([self.timed_body(delay, str(i))
                                          for i, delay in enumerate((.25, .75, .25))], 2, {"2": ["0", "1"]}))
        for row in results:
            self.assert_clean(row)
            self.assertIsNone(row["failure"], row)
        spans = [json.loads(row["stdout"]) for row in results]
        events = sorted([(row["start"], 1) for row in spans] + [(row["end"], -1) for row in spans])
        active = maximum = 0
        for _, delta in events:
            active += delta
            maximum = max(maximum, active)
        self.assertEqual(maximum, 2)
        self.assertEqual(active, 0)
        self.assertLess(max(row["start"] for row in spans), max(row["end"] for row in spans))
        self.assertLess(spans[2]["start"], spans[1]["end"], "must not schedule in barrier-sized waves")

    def test_escaped_child_cleanup_does_not_kill_peer_or_unrelated_process(self):
        outsider = subprocess.Popen(["/bin/sleep", "3"])
        sibling = subprocess.Popen([*self.prefix, "/bin/sleep", "3"], cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.settle, outsider)
        self.addCleanup(self.settle, sibling)
        escaped = ("import json,subprocess,sys\n"
                   "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(15)'],start_new_session=True)\n"
                   "print(json.dumps({'escaped':p.pid}))\n")
        rows = self.result(self.launch([escaped, self.timed_body(.35, "peer")], 2))
        for row in rows:
            self.assert_clean(row)
            self.assertEqual(row["failure"], "GRAPH_DESCENDANTS_OUTLIVED_ENGINE", row)
        self.assertIn("end", json.loads(rows[1]["stdout"]))
        child = json.loads(rows[0]["stdout"])["escaped"]
        info = graph.DarwinProcesses().info(child)
        self.assertTrue(info is None or info.status == 5)
        self.assertIsNone(outsider.poll())
        self.assertIsNone(sibling.poll())

    def test_cancel_cleans_running_members_and_does_not_spawn_queued_member(self):
        process = self.launch([self.timed_body(15, str(i)) for i in range(3)], 2, {"2": ["0", "1"]})
        deadline = time.monotonic() + 5
        while not all((self.root / str(i)).exists() for i in (0, 1)) and time.monotonic() < deadline:
            self.assertIsNone(process.poll())
            time.sleep(.01)
        self.assertTrue(all((self.root / str(i)).exists() for i in (0, 1)))
        os.kill(int((self.root / "root.pid").read_text()), signal.SIGINT)
        rows = self.result(process)
        self.assertFalse((self.root / "2").exists())
        for row in rows:
            if "scope" in row:
                self.assert_clean(row)
                self.assertIsNotNone(row["failure"])
            else:
                self.assertIn("CANCELLED_BEFORE_SPAWN", row["error"])

    def test_spawn_failure_unblocks_other_members_and_cleans(self):
        rows = self.result(self.launch([self.timed_body(.2, "peer"), None], 2))
        self.assertIn("No such file", rows[1]["error"])
        if "scope" in rows[0]:
            self.assert_clean(rows[0])
            self.assertIsNotNone(rows[0]["failure"])
        else:
            self.assertIn("CANCELLED_BEFORE_SPAWN", rows[0]["error"])


@unittest.skipUnless(sys.platform == "darwin" and NATIVE, "explicit native full-caller fixture")
class NativeBatchCallerTests(unittest.TestCase):
    def setUp(self):
        import test_full_core
        from codex_collaboration_harness import embedded_nokiy as caller
        self.fixture = test_full_core.FullCoreTests()
        with patch.object(tempfile, "tempdir", os.environ.get("NOKIY_GRAPH_TEST_ROOT")):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture.f
        self.root = f.root
        # Synthetic runtime and authority only; never use real credentials or DCF.
        (f.workspace / "input.txt").write_text("read-only fixture\n")
        f.context, f.jspace = f._write_context()
        contract = json.loads(f.jspace.read_text())
        contract.update(read_scopes=["input.txt"], allowed_operations=["read"],
                        denied_operations=["command", "create", "modify", "delete"])
        f.jspace.write_text(json.dumps(contract))
        f._canonical_v2_context()
        contract = json.loads(f.jspace.read_text())
        contract["command_templates"] = []
        authorization = {key: contract.get(key) for key in (
            "repo_root", "matched_surface_ids", "read_scopes", "write_scopes",
            "allowed_operations", "denied_operations", "command_templates", "declared_targets",
            "expansion", "command_effect_policy")}
        authorization.update(schema_version="jspace_authorization_v1", required_domain_bindings={})
        contract["authorization_semantic_sha256"] = caller._canonical_sha256(authorization)
        contract.pop("content_sha256")
        contract["content_sha256"] = caller._canonical_sha256(contract)
        f.jspace.write_text(json.dumps(contract))
        capsule = json.loads(f.context.read_text())
        capsule["jspace_semantic_sha256"] = contract["authorization_semantic_sha256"]
        capsule.pop("semantic_sha256")
        capsule["semantic_sha256"] = caller._canonical_sha256(capsule)
        f.context.write_text(json.dumps(capsule))
        cli = f.runtime / "tura_exec"
        cli.write_text(cli.read_text().replace("jspace['semantic_sha256']", "jspace['authorization_semantic_sha256']")
                       .replace("prompt=sys.stdin.read()", "started=time.monotonic()\ntime.sleep(.2)\nprompt=sys.stdin.read()")
                       .replace("observation = None", "(state/'fixture-span.json').write_text(json.dumps({'start':started,'end':time.monotonic()}))\nobservation = None"))
        self.fixture._image()
        self.members = []
        for index in range(3):
            path = f._write_request(prompt=f"STATUS_ONLY\nmember {index}", require_tool_call=False)
            data = json.loads(path.read_text())
            artifacts = f.root / f"member-{index}"
            artifacts.mkdir()
            data.update(execution_profile="direct", artifact_root=str(artifacts))
            path.write_text(json.dumps(data))
            request = caller.load_request(path)
            self.members.append({"request_path": str(path), "request_file_sha256": self.sha(path),
                                 "request_id": request.request_id, "artifact_root": str(artifacts)})
        self.plan = f.root / "plan.json"
        self.plan.write_text(json.dumps({"schema_version": "nokiy_direct_batch_plan_v1",
                                        "members": self.members, "max_parallel_workers": 2}))
        self.prefix = [str(Path(NATIVE).resolve(strict=True)), "sandbox", "--permission-profile",
            "nokiy_benchmark", "--include-managed-config", "-c", 'default_permissions="nokiy_benchmark"',
            "-c", 'permissions.nokiy_benchmark.extends=":workspace"',
            "-c", "permissions.nokiy_benchmark.network.enabled=true", "--"]

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def invoke(self, operation):
        code = (f"import sys;sys.path.insert(0,{SOURCE!r});"
                "from codex_collaboration_harness.embedded_nokiy import main;raise SystemExit(main())")
        return subprocess.run([*self.prefix, sys.executable, "-I", "-B", "-c", code,
            operation, "--plan", str(self.plan), "--plan-sha256", self.sha(self.plan)],
            cwd=self.root, capture_output=True, text=True, timeout=20)

    def test_unproven_results_stay_blocked_after_parallel_cleanup_and_recovery(self):
        result = self.invoke("run-batch")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["status"], "BLOCKED", response)
        self.assertEqual(response["first_typed_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
        snapshots = {}
        spans = []
        for member in self.members:
            run = Path(member["artifact_root"]) / member["request_id"]
            terminal = json.loads((run / "terminal.json").read_text())
            self.assertEqual(terminal["status"], "RESULT_AVAILABLE")
            self.assertTrue(terminal["cleanup_pass"], terminal)
            self.assertEqual(terminal["cleanup"]["cleanup_scope"], "whole_native_batch")
            spans.append(json.loads((run / "execution-state/fixture-span.json").read_text()))
            for name in ("terminal.json", "core.jsonl", "execution-state/cli-reached"):
                snapshots[run / name] = self.sha(run / name)
        self.assertTrue(any(max(left["start"], right["start"]) < min(left["end"], right["end"])
                            for index, left in enumerate(spans) for right in spans[index + 1:]))
        recovered = self.invoke("read-batch")
        self.assertEqual(recovered.returncode, 2, recovered.stdout + recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["first_typed_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
        self.assertEqual(snapshots, {path: self.sha(path) for path in snapshots})
        self.assertFalse((self.fixture.f.workspace / "answer.txt").exists())


@unittest.skipUnless(sys.platform == "darwin" and NATIVE, "explicit compiled native batch fixture")
class CompiledNativeBatchTests(unittest.TestCase):
    def test_compiled_workers_deliver_and_recover_without_reexecution(self):
        import test_responses_evidence_handoff as handoff
        from codex_collaboration_harness import embedded_nokiy as caller, local_context

        fixture = handoff.RuntimeEvidenceHandoffTests()
        with patch.object(tempfile, "tempdir", os.environ.get("NOKIY_GRAPH_TEST_ROOT")):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        f = fixture.f
        source = f.workspace / "input.txt"
        source.write_text("parallel compiled source evidence\n")
        before = NativeBatchCallerTests.sha(source)
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        arrivals = []

        def response(body):
            if "parallel compiled source evidence" in json.dumps(body["input"]):
                return handoff._tool_message([
                    handoff._command("task_status", json.dumps({"status": "done",
                        "final_reply": "parallel compiled fixture complete"}), 1)], 10)
            with lock:
                index = len(arrivals)
                arrivals.append(time.monotonic())
            if index < 2:
                barrier.wait(timeout=10)
            return handoff._tool_message([
                handoff._command("source_read", json.dumps({"path": "input.txt", "start_line": 1,
                    "end_line": 1, "expected_sha256": before}), 1)], index)

        with patch.object(handoff, "HTTPServer", ThreadingHTTPServer), \
                handoff._loopback_provider([response] * 6) as (url, requests, errors, _), \
                handoff._offline_runtime_environment(fixture.native_environment, url):
            fixture._loopback_image(url)
            action = {"mission": {"mission_id": "native-batch", "task_id": "compiled-read",
                "mode": "DELIVERY", "objective": "Read the fixture and finish",
                "current_predicate": "parallel result delivery unverified"},
                "context_summary": "Disposable fixture; loopback provider; no cloud inference.",
                "operations": ["read", "command"], "source_read": True,
                "read_scopes": ["input.txt"], "write_scopes": [], "target_paths": [],
                "command_templates": [], "forbidden_effects": ["network", "delete", "modify"]}
            capsule, contract = local_context.compile_context(f.workspace, action, artifact_root=f.artifacts)
            f.context.write_text(json.dumps(capsule))
            f.jspace.write_text(json.dumps(contract))
            members = []
            for index in range(3):
                path = f._write_request(prompt=f"Read input.txt and finish member {index}.")
                data = json.loads(path.read_text())
                artifacts = f.root / f"member-{index}"
                artifacts.mkdir()
                data.update(execution_profile="direct", authority_effect="none", model=handoff.MODEL,
                    reasoning_effort="max", service_tier="default", timeout_seconds=30,
                    terminal_delivery="assistant_reply", artifact_root=str(artifacts),
                    initial_task_state={"task_group": "native batch fixture", "task_type": ["debug"]})
                path.write_text(json.dumps(data))
                request = caller.load_request(path)
                members.append({"request_path": str(path), "request_file_sha256": NativeBatchCallerTests.sha(path),
                                "request_id": request.request_id, "artifact_root": str(artifacts)})
            plan = f.root / "plan.json"
            plan.write_text(json.dumps({"schema_version": "nokiy_direct_batch_plan_v1",
                                        "members": members, "max_parallel_workers": 2}))
            # Deny real auth; the complete fixture provider inventory targets loopback.
            auth = Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")) / "auth.json"
            permissions = ["-c", 'default_permissions="nokiy_benchmark"',
                "-c", 'permissions.nokiy_benchmark.extends=":workspace"',
                "-c", "permissions.nokiy_benchmark.network.enabled=true"]
            denied = sorted({str(auth), str(auth.resolve(strict=True))})
            filesystem = "{" + ",".join(json.dumps(path) + '="deny"' for path in denied) + "}"
            permissions += ["-c", "permissions.nokiy_benchmark.filesystem=" + filesystem]
            settings = {"CODEX_THREAD_ID": fixture.native_environment["CODEX_THREAD_ID"],
                "OPENAI_API_KEY": handoff.DUMMY_API_KEY, "OPENAI_ACCOUNT_ID": handoff.DUMMY_ACCOUNT_ID,
                "OPENAI_TOKEN_EXPIRES": "4102444800000", "OPENAI_CODEX_ENDPOINT": url + handoff.CODEX_RESPONSES_PATH,
                "NO_PROXY": "127.0.0.1,localhost"}
            code = f"import sys;sys.path.insert(0,{SOURCE!r})\n" + f'''
import errno,json,os
os.environ.update(json.loads({json.dumps(settings)!r}))
for path in {denied!r}:
    try: fd=os.open(path,os.O_RDONLY)
    except OSError as error: assert error.errno in (errno.EACCES,errno.EPERM), error
    else:
        os.close(fd)
        raise AssertionError('real credential read was not denied')
from codex_collaboration_harness.embedded_nokiy import main
raise SystemExit(main())
'''
            command = [str(Path(NATIVE).resolve()), "sandbox", "--permission-profile", "nokiy_benchmark",
                "--include-managed-config", *permissions, "--", sys.executable, "-I", "-B", "-c", code]

            def invoke(operation):
                result = subprocess.run([*command, operation, "--plan", str(plan), "--plan-sha256",
                    NativeBatchCallerTests.sha(plan)], cwd=f.root, capture_output=True, text=True, timeout=45)
                if result.returncode:
                    diagnostics = {"returncode": result.returncode, "stderr": result.stderr,
                                   "fixture_provider_errors": errors, "provider_requests": len(requests)}
                    for member in members:
                        run = Path(member["artifact_root"]) / member["request_id"]
                        diagnostics[member["request_id"]] = {
                            name: (run / name).read_text()[-5000:]
                            for name in ("supervision.json", "core.stderr", "core.jsonl") if (run / name).is_file()}
                    self.fail(json.dumps(diagnostics, indent=2))
                value = json.loads(result.stdout)
                self.assertEqual(value["status"], "RESULT_AVAILABLE", value)
                return value

            result = invoke("run-batch")
            self.assertEqual(len(arrivals), 3)
            self.assertFalse(errors, errors)
            snapshots = {}
            for row in result["members"]:
                terminal = row["terminal"]
                self.assertEqual(terminal["result_inspection"]["status"], "EVIDENCE_VERIFIED")
                self.assertEqual(terminal["result_inspection"]["cleanup"]["supervisor_pid"], "current_root_settled")
                self.assertEqual(terminal["cleanup"]["cleanup_scope"], "whole_native_batch")
                self.assertEqual(terminal["result_text"], "parallel compiled fixture complete")
                run = Path(row["artifact_root"]) / row["request_id"]
                for name in ("terminal.json", "core.jsonl"):
                    snapshots[run / name] = NativeBatchCallerTests.sha(run / name)
            invoke("read-batch")
            self.assertEqual(len(requests), 6, "recovery must never call the provider")
            self.assertEqual(snapshots, {path: NativeBatchCallerTests.sha(path) for path in snapshots})
            self.assertEqual(NativeBatchCallerTests.sha(source), before)


if __name__ == "__main__":
    unittest.main()
