# SPDX-License-Identifier: MIT
"""Real caller/service processes with synthetic core binaries; no provider send."""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import tracemalloc
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_embedded_nokiy as fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core as core
from codex_collaboration_harness import result_inspection as inspector


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
# nokiy_execution_budget_v1
name=pathlib.Path(sys.argv[0]).name
if len(sys.argv) == 2 and sys.argv[1] == 'command-receipt-capabilities' and name == 'tura_router':
    assert 'TURA_COMMAND_RECEIPT_ROOT' not in os.environ
    assert 'TURA_COMMAND_RECEIPT_WORKSPACE' not in os.environ
    print('nokiy_command_receipt_binding_v1')
    sys.exit(0)
if len(sys.argv) == 2 and sys.argv[1] == 'command-receipt-preflight' and name == 'tura_router':
    root=pathlib.Path(os.environ['TURA_COMMAND_RECEIPT_ROOT'])
    workspace=pathlib.Path(os.environ['TURA_COMMAND_RECEIPT_WORKSPACE'])
    assert workspace.is_dir() and workspace == workspace.resolve(strict=True)
    assert root.is_dir() and root == root.resolve(strict=True) and not root.is_relative_to(workspace)
    receipts=root/'command_receipts'
    receipts.mkdir(mode=0o700,exist_ok=True)
    assert receipts.is_dir() and not receipts.is_symlink()
    sys.exit(0)
if name == 'tura_router': assert sys.argv[1:] == ['serve-socket']
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
if 'NOKIY_EXECUTION_BUDGET' in os.environ:
    (state/'execution-budget.json').write_text(os.environ['NOKIY_EXECUTION_BUDGET'])
prompt=sys.stdin.read()
# The mock scenario is the first line; caller capability guidance is not a scenario.
scenario=prompt.split('\n',1)[0]
if scenario=='TIMEOUT':
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=True)
    (state/'grandchild.pid').write_text(str(child.pid))
    time.sleep(120)
if scenario=='FAIL': sys.exit(2)
if scenario=='TRAJECTORY_SPOOL':
    row=json.dumps({'type':'diagnostic','text':'x'*65536})
    for _ in range(1025): print(row)
if scenario=='FAIL_AFTER_EDIT':
    pathlib.Path(value('-C'),'answer.txt').write_text('effect before failure\n')
    sys.exit(2)
if scenario=='STATUS_ONLY':
    print(json.dumps({'type':'item.completed','item':{'type':'command_execution','status':'completed',
        'aggregated_output':json.dumps({'task_status':{'task_group':'fixture'}})}}))
elif scenario=='UNVERIFIED_COMMAND':
    print(json.dumps({'type':'item.completed','item':{'type':'command_execution','status':'completed',
        'aggregated_output':json.dumps({'stdout':'ok'})}}))
else:
    target=pathlib.Path(value('-C'),'answer.txt')
    kind='update' if target.exists() else 'add'
    target.write_text('full-core result\n')
    item={'type':'file_change','id':'fixture-edit','status':'completed',
          'changes':[{'kind':kind,'path':str(target)}]}
    if scenario=='MALFORMED_EDIT_EVENT': item.pop('changes')
    print(json.dumps({'type':'item.completed','item':item}))
message = ('long result: ' + '\u6e2c\u8a66' * 600
           if scenario == 'LONG_RESULT' else 'finished edit and verification')
print(json.dumps({'type':'item.completed','item':{'type':'assistant_message','text':message}}))
observation = None
if scenario in ('ACTUAL_DEFAULT', 'ACTUAL_PRIORITY'):
    actual = 'default' if scenario == 'ACTUAL_DEFAULT' else 'priority'
    observation = {'schema_version':'provider_observation_summary_v1', 'source':'provider_response',
        'runtime_count':1, 'observation_count':1, 'conflict_count':0,
        'model':{'value':'gpt-6', 'observed_count':1, 'distinct_count':1, 'distinct_values':['gpt-6']},
        'service_tier':{'value':actual, 'observed_count':1, 'distinct_count':1, 'distinct_values':[actual]}}
print(json.dumps({'type':'turn.completed','status':'completed','usage':{'total_tokens':11},
    'model':value('-m'),'agent':value('-a'),'session_id':value('--session-id'),'cwd':value('-C'),
    'reasoning_effort':value('--model-reasoning-effort'),'service_tier':value('--service-tier'),
    'provider_observation':observation}))
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

    def test_receipt_binding_replaces_inherited_values_and_uses_request_state(self):
        request = self._request()
        state = request.artifact_root / request.request_id / "execution-state"
        with patch.dict(os.environ, {core.RECEIPT_ROOT_ENV: "/tmp/injected",
                                  core.RECEIPT_WORKSPACE_ENV: "/tmp/injected"}):
            env = core._environment(request, caller.verify_runtime_image(
                request.runtime_image, required_artifacts=core.REQUIRED), state)
            self.assertEqual(env[core.RECEIPT_ROOT_ENV], str(state))
            self.assertEqual(env[core.RECEIPT_WORKSPACE_ENV], str(request.workspace))
            self.assertNotEqual(env[core.RECEIPT_ROOT_ENV], "/tmp/injected")
            if sys.platform == "darwin":
                self.assertEqual(core.prepare(request)[0]["status"], "READY")

    def test_nested_or_equal_artifact_root_blocks_before_process_or_request_consumption(self):
        nested = self.f.workspace / "worker"
        nested.mkdir()
        for profile in ("direct", "balanced"):
            for root in (self.f.workspace, nested):
                with self.subTest(profile=profile, artifact_root=root):
                    value = self._request(profile).to_wire(include_identity=False)
                    value["artifact_root"] = str(root)
                    request = caller.decode_request(value)
                    with patch.object(core.subprocess, "run") as process_run, \
                            patch.object(core.subprocess, "Popen") as process_open:
                        for operation in (caller.preflight, caller.execute):
                            with self.assertRaises(caller.EmbeddedNokiyError) as raised:
                                operation(request)
                            self.assertEqual(raised.exception.code,
                                             "NOKIY_FULL_CORE_ARTIFACT_WORKSPACE_OVERLAP")
                            self.assertIn("task-owned sibling artifact directory outside workspace",
                                          raised.exception.detail)
                        process_run.assert_not_called()
                        process_open.assert_not_called()
                    self.assertFalse((root / request.request_id).exists())
                    self.assertFalse((self.f.workspace / "answer.txt").exists())

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_sibling_artifact_root_is_ready_without_consuming_request(self):
        request = self._request()
        self.assertEqual(request.artifact_root.parent, request.workspace.parent)
        with patch.object(core.subprocess, "run", wraps=subprocess.run) as process_run:
            self.assertEqual(caller.preflight(request)["status"], "READY")
            process_run.assert_called_once()
            self.assertEqual(process_run.call_args.args[0],
                             [str(self.f.runtime / "tura_router"), "command-receipt-capabilities"])
        self.assertFalse((request.artifact_root / request.request_id).exists())

    def test_existing_nested_terminal_reads_without_new_layout_validation(self):
        value = self._request().to_wire(include_identity=False)
        value["artifact_root"] = str(self.f.workspace)
        request = caller.decode_request(value)
        run = request.artifact_root / request.request_id
        run.mkdir()
        terminal = {"schema_version": caller.TERMINAL_SCHEMA_VERSION,
                    "request_id": request.request_id, "request_sha256": request.request_sha256,
                    "status": "BLOCKED", "cleanup_pass": True,
                    "first_typed_blocker": "NOKIY_FULL_CORE_COMMAND_RECEIPT_STORE_INVALID"}
        path = run / "terminal.json"
        path.write_text(json.dumps(terminal))
        before = path.read_bytes()
        with patch.object(core, "prepare", side_effect=AssertionError("must not re-prepare")), \
                patch.object(core.subprocess, "run") as process_run, \
                patch.object(core.subprocess, "Popen") as process_open:
            self.assertEqual(caller.read_terminal(request.artifact_root, request.request_id), terminal)
            self.assertEqual(caller.execute(request), terminal)
            process_run.assert_not_called()
            process_open.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_missing_command_receipt_capability_blocks_before_provider(self):
        router = self.f.runtime / "tura_router"
        router.write_text(router.read_text().replace("print('nokiy_command_receipt_binding_v1')",
                                                    "print('unknown-version')"))
        self._image()
        request = self._request()
        ready = core.prepare(request)[0]
        self.assertEqual(ready["status"], "BLOCKED")
        self.assertEqual(ready["first_typed_blocker"],
                         "NOKIY_FULL_CORE_COMMAND_RECEIPT_BINDING_REQUIRED")
        self.assertFalse((request.artifact_root / request.request_id).exists())

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_workspace_tura_symlink_is_untouched_by_bound_state_preflight(self):
        target = self.f.root / "runtime-layout"
        target.mkdir()
        (self.f.workspace / ".tura").symlink_to(target, target_is_directory=True)
        request = self._request()
        result = caller.execute(request)
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)
        self.assertTrue((self.f.workspace / ".tura").is_symlink())
        self.assertEqual((self.f.workspace / ".tura").resolve(), target)
        self.assertFalse((target / "run").exists())
        self.assertTrue((request.artifact_root / request.request_id /
                         "execution-state/command_receipts").is_dir())

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_invalid_receipt_child_blocks_before_cli_launch(self):
        outside = self.f.root / "outside"
        outside.mkdir()
        router = self.f.runtime / "tura_router"
        router.write_text(router.read_text().replace(
            "receipts.mkdir(mode=0o700,exist_ok=True)",
            f"receipts.symlink_to({str(outside)!r},target_is_directory=True)"))
        self._image()
        request = self._request()
        result = caller.execute(request)
        self.assertEqual(result["status"], "BLOCKED", result)
        state = request.artifact_root / request.request_id / "execution-state"
        self.assertFalse((state / "cli-reached").exists())
        self.assertFalse((self.f.workspace / "answer.txt").exists())
        self.assertEqual(list(outside.iterdir()), [])

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

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_verified_read_projection_reaches_runtime_without_mutating_contract(self):
        jspace = json.loads(self.f.jspace.read_text())
        jspace.update(source_read=True, read_scopes=["input.txt"])
        jspace.pop("semantic_sha256")
        jspace["semantic_sha256"] = caller._canonical_sha256(jspace)
        self.f.jspace.write_text(json.dumps(jspace))
        capsule = json.loads(self.f.context.read_text())
        capsule["jspace_semantic_sha256"] = jspace["semantic_sha256"]
        capsule["evidence_refs"] = []
        capsule.pop("semantic_sha256")
        capsule["semantic_sha256"] = caller._canonical_sha256(capsule)
        self.f.context.write_text(json.dumps(capsule))
        cli = self.f.runtime / "tura_exec"
        cli.write_text(cli.read_text().replace("prompt=sys.stdin.read()", """prompt=sys.stdin.read()
assert 'nokiy_source_read_presentation_v1' in prompt
assert json.loads(prompt.splitlines()[3])['read_scopes'] == ['input.txt']
assert capsule['evidence_refs'] == []
"""))
        self._image()
        request = self._request()
        result = caller.execute(request)
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)
        run = request.artifact_root / request.request_id
        self.assertEqual(json.loads((run / "jspace.json").read_text()), jspace)
        self.assertEqual(json.loads((run / "capsule.json").read_text()), capsule)
        self.assertEqual(json.loads((run / "request-identity.json").read_text())["prompt"], request.prompt)
        self.assertTrue(result["cleanup_pass"])
        with patch.object(core, "prepare", side_effect=AssertionError("must recover, not replay")):
            self.assertEqual(caller.execute(request), result)

    def _tool_events(self, observations, summary=None, tier="default", request=None):
        request = request if request is not None else self._request()
        path = self.f.root / "observations.jsonl"
        events = [{"type": "item.completed", "item": item} for item in observations]
        events.append({"type": "turn.completed", "status": "completed",
                       "model": "codex/" + request.model, "agent": request.execution_profile,
                       "session_id": "full-" + request.request_sha256, "cwd": str(request.workspace),
                       "reasoning_effort": request.reasoning_effort, "service_tier": tier,
                       "usage": {"total_tokens": 11}, "provider_observation": summary})
        path.write_text("\n".join(json.dumps(event) for event in events))
        return core._events(path, request.max_trajectory_bytes, request)

    def _spooling_request(self, limit=None, prompt="edit and verify"):
        value = self._request(prompt=prompt).to_wire(include_identity=False)
        value.update(max_trajectory_bytes=limit, timeout_seconds=None)
        request = caller.decode_request(value)
        terminal = {"type": "turn.completed", "status": "completed", "usage": {"total_tokens": 11},
                    **core._terminal_identity(request)}
        return request, terminal

    def test_streamed_trajectory_above_former_cap_and_explicit_cap_failure(self):
        request, terminal = self._spooling_request()
        path = self.f.root / "large-core.jsonl"
        row = caller._canonical_bytes({"type": "diagnostic", "text": "x" * 65536}) + b"\n"
        command = {"type": "item.completed", "item": {"type": "command_execution", "status": "completed",
                   "command": "echo ok", "exit_code": 0}}
        count = 0
        with path.open("wb") as output:
            while output.tell() <= caller.MAX_TRAJECTORY_BYTES:
                output.write(row)
                count += 1
            for event in (command, {"type": "item.completed", "item": {"type": "assistant_message", "text": "complete"}}, terminal):
                output.write(caller._canonical_bytes(event) + b"\n")
        self.assertGreater(path.stat().st_size, caller.MAX_TRAJECTORY_BYTES)
        tracemalloc.start()
        try:
            with patch.object(Path, "read_text", side_effect=AssertionError("bulk trajectory read")), \
                    patch.object(Path, "read_bytes", side_effect=AssertionError("bulk artifact read")):
                observed = core._events(path, request.max_trajectory_bytes, request)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024)
        self.assertTrue(observed["turn_completed"])
        self.assertEqual(observed["final_text"], "complete")
        record = observed["command_evidence"]["records"][0]
        self.assertEqual(record["event_index"], count)
        self.assertEqual(record["event_sha256"], caller._canonical_sha256(command))
        finite, _ = self._spooling_request(caller.MAX_TRAJECTORY_BYTES)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "TRAJECTORY_LIMIT_EXCEEDED"):
            core._events(path, finite.max_trajectory_bytes, finite)

    def test_large_last_message_roundtrip_never_enters_supervisor_summary(self):
        request, terminal = self._spooling_request()
        text = "head: " + "\u6e2c\u8a66" * 600000 + " :tail"
        path = self.f.root / "large-message.jsonl"
        with path.open("wb") as output:
            for event in ({"type": "item.completed", "item": {"type": "assistant_message", "text": text}},
                          {"type": "diagnostic", "text": "after the message"}, terminal):
                output.write(caller._canonical_bytes(event) + b"\n")
        observed = core._events(path, None, request)
        self.assertTrue(observed["result_truncated"])
        self.assertEqual(observed["final_text"], caller._bounded_preview(text, request.max_result_bytes)[0])
        result_path = path.parent / "last-message.txt"
        self.assertEqual(result_path.read_text(encoding="utf-8"), text)
        self.assertEqual(observed["result_artifact"], {
            "path": str(result_path), "sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text.encode())})
        encoded = core._summary_json(observed, core.MAX_ENGINE_SUMMARY_BYTES).encode()
        self.assertLess(len(encoded), core.MAX_ENGINE_SUMMARY_BYTES)
        projected = core._result_projection(path.parent, observed["final_text"], True,
                                             observed["result_artifact"], request.max_result_bytes)
        self.assertEqual(projected, (observed["final_text"], True, observed["result_artifact"]))
        for bad in (None, dict(observed["result_artifact"], path=str(self.f.root / "foreign.txt")),
                    dict(observed["result_artifact"], bytes=observed["result_artifact"]["bytes"] + 1),
                    dict(observed["result_artifact"], sha256="0" * 64)):
            with self.subTest(reference=bad), self.assertRaises(caller.EmbeddedNokiyError):
                core._result_projection(path.parent, observed["final_text"], True, bad, request.max_result_bytes)

    def test_streamed_malformed_duplicate_and_foreign_terminals_rejected(self):
        request, terminal = self._spooling_request()
        path = self.f.root / "invalid-core.jsonl"
        cases = [(b'{"type":\n', "TRAJECTORY_INVALID_JSON"),
                 (b'{"type":"x","type":"y"}\n', "TRAJECTORY_INVALID_JSON"),
                 (b'{"value":NaN}\n', "TRAJECTORY_INVALID_JSON"),
                 (b'[]\n', "TRAJECTORY_INVALID_JSON"),
                 (b'', "NOKIY_FULL_CORE_TERMINAL_INVALID"),
                 (caller._canonical_bytes(terminal) + b"\n" + caller._canonical_bytes(terminal),
                  "NOKIY_FULL_CORE_TERMINAL_INVALID")]
        cases.extend((caller._canonical_bytes(dict(terminal, **{field: "foreign"})),
                      "NOKIY_FULL_CORE_TERMINAL_IDENTITY_MISMATCH")
                     for field in ("model", "agent", "session_id", "cwd", "reasoning_effort", "service_tier"))
        for raw, code in cases:
            with self.subTest(code=code, raw=raw[:80]):
                path.write_bytes(raw)
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, code):
                    core._events(path, None, request)

    def test_event_indices_and_hashes_keep_unfiltered_jsonl_order(self):
        request, terminal = self._spooling_request()
        command = {"type": "item.completed", "item": {"type": "command_execution", "status": "completed",
                   "command": "echo ok", "exit_code": 0}}
        events = [{"type": "diagnostic"}, {"type": "item.started"},
                  {"type": "item.completed", "item": {"type": "command_execution", "command_type": "task_status"}},
                  command, {"type": "diagnostic"}, command, terminal]
        path = self.f.root / "indexed-core.jsonl"
        path.write_bytes(b"\n".join(caller._canonical_bytes(event) + b"\n" for event in events))
        evidence = core._events(path, None, request)["command_evidence"]
        self.assertEqual([r["event_index"] for r in evidence["records"]], [3, 5])
        self.assertEqual([r["event_sha256"] for r in evidence["records"]],
                         [caller._canonical_sha256(command)] * 2)

    def test_streamed_file_evidence_keeps_original_event_index_and_hash(self):
        request, _ = self._spooling_request()
        target = request.workspace / "answer.txt"
        target.write_text("result\n")
        contract = caller._load_json(self.f.jspace, limit=caller.MAX_CONTEXT_BYTES, code="TEST_CONTRACT_INVALID")
        event = {"type": "item.completed", "item": {"type": "file_change", "id": "edit-1", "status": "completed",
                 "changes": [{"kind": "add", "path": str(target)}]}}
        events = [{"type": "diagnostic"}, event, {"type": "diagnostic"}]
        proof = core.file_change_evidence.produce(iter(events), request, contract)
        self.assertEqual(proof["records"][0]["event_index"], 1)
        self.assertEqual(proof["records"][0]["event_sha256"], caller._canonical_sha256(event))
        terminal = {"request_id": request.request_id, "request_sha256": request.request_sha256,
                    "native_thread_id": request.native_thread_id}
        verified = core.file_change_evidence.verify(proof, iter(events), terminal, request.to_wire(), contract)
        self.assertEqual(verified, {"events": 1, "targets": 1})

    def test_artifact_readback_rejects_checksum_size_type_symlink_and_drift(self):
        path = self.f.root / "artifact.txt"
        raw = b"x" * 131072
        path.write_bytes(raw)
        reference = core._record_artifact(path)
        for bad in (dict(reference, sha256="0" * 64), dict(reference, bytes=len(raw) + 1)):
            with self.subTest(reference=bad), self.assertRaisesRegex(caller.EmbeddedNokiyError, "ARTIFACT_HASH_MISMATCH"):
                core._record_artifact(path, reference=bad)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "ARTIFACT_CHANGED"):
            with core._ArtifactReader(path, reference=reference) as source:
                chunks = source.chunks()
                next(chunks)
                replacement = self.f.root / "replacement.txt"
                replacement.write_bytes(raw)
                os.replace(replacement, path)  # same bytes, different physical file
                for _ in chunks:
                    pass
        target = self.f.root / "target.txt"
        target.write_bytes(raw)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "UNSAFE_FILE"):
            core._record_artifact(path, reference=reference)
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "UNSAFE_FILE"):
            core._record_artifact(path, reference=reference)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "FILE_UNAVAILABLE"):
            core._record_artifact(self.f.root / "missing.txt")

    def test_disk_evidence_reiteration_rejects_physical_replacement(self):
        path = self.f.root / "reiterated.jsonl"
        raw = caller._canonical_bytes({"type": "diagnostic"}) + b"\n"
        path.write_bytes(raw)
        events = core._Trajectory(path)
        self.assertEqual(core._command_evidence(events)["total_count"], 0)
        replacement = self.f.root / "reiterated-replacement.jsonl"
        replacement.write_bytes(raw)
        os.replace(replacement, path)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "ARTIFACT_CHANGED"):
            core._command_evidence(events)

    def test_supervisor_summary_budget_is_finite_and_independent_of_trajectory_cap(self):
        for limit in (None, 1024):
            with self.subTest(limit=limit):
                request, _ = self._spooling_request(limit)
                spec = self.f.root / "summary-spec.json"
                spec.write_bytes(caller._canonical_bytes({"request": request.to_wire(include_identity=False)}))
                outcome = SimpleNamespace(stdout=b'{}', scope={}, failure=None, returncode=0)
                stdout = io.StringIO()
                with patch.object(sys, "argv", [core.MODULE, "--supervise", str(spec), "--parent-pid", "123"]), \
                        patch.object(core, "_supervisor_environment", return_value={}), \
                        patch.object(core, "_verifier_fd", return_value=()), \
                        patch.object(core.signal, "signal"), patch.object(core, "supervise", return_value=outcome) as supervise, \
                        redirect_stdout(stdout):
                    self.assertEqual(core.main(), 0)
                self.assertEqual(supervise.call_args.kwargs["max_stdout"], core.MAX_ENGINE_SUMMARY_BYTES)
                self.assertIs(type(supervise.call_args.kwargs["max_stdout"]), int)
                self.assertLess(len(stdout.getvalue().encode()), core.MAX_SUPERVISION_BYTES)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_full_caller_spools_above_former_cap_and_keeps_explicit_finite_stop(self):
        from codex_collaboration_harness import result_inspection
        request, _ = self._spooling_request(prompt="TRAJECTORY_SPOOL")
        result = caller.execute(request)
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)
        self.assertGreater(result["trajectory_artifact"]["bytes"], caller.MAX_TRAJECTORY_BYTES)
        self.assertLess(result["supervision_artifact"]["bytes"], core.MAX_SUPERVISION_BYTES)
        self.assertTrue(result["cleanup_pass"], result)
        run = request.artifact_root / request.request_id
        proof = json.loads((run / "file-change-evidence.json").read_text())
        self.assertEqual(proof["records"][0]["event_index"], 1025)
        with patch.object(result_inspection.os, "kill", side_effect=ProcessLookupError()):
            inspection = result_inspection.inspect(request.artifact_root, request.request_id, request.native_thread_id)
        self.assertEqual(inspection["status"], "EVIDENCE_VERIFIED", inspection)
        finite, _ = self._spooling_request(65536, prompt="TRAJECTORY_SPOOL")
        blocked = caller.execute(finite)
        self.assertEqual(blocked["status"], "BLOCKED", blocked)
        self.assertEqual(blocked["first_typed_blocker"], "NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED")
        self.assertTrue(blocked["cleanup_pass"], blocked)

    def test_provider_summary_is_separate_from_requested_tier_and_command_evidence(self):
        def summary(tier):
            return {"schema_version": "provider_observation_summary_v1", "source": "provider_response",
                    "runtime_count": 1, "observation_count": 1, "conflict_count": 0,
                    "model": {"value": "gpt-6", "observed_count": 1, "distinct_count": 1,
                              "distinct_values": ["gpt-6"]},
                    "service_tier": {"value": tier, "observed_count": 1, "distinct_count": 1,
                                     "distinct_values": [tier]}}
        from dataclasses import replace
        original = self._request()
        request = replace(original, service_tier="priority")
        for tier in ("default", "priority"):
            with self.subTest(tier=tier):
                observed = self._tool_events([{"type":"command_execution", "status":"completed", "exit_code":0}], summary(tier), "priority", request)
                self.assertEqual(observed["provider_observation"]["service_tier"]["value"], tier)
                self.assertEqual(observed["provider_observation"]["model"]["value"], "gpt-6")
                self.assertEqual(observed["usage"], {"total_tokens": 11})
                self.assertEqual(observed["command_evidence"]["total_count"], 1)
        legacy = self._tool_events([], tier="priority", request=request)
        self.assertIsNone(legacy["provider_observation"]["service_tier"]["value"])
        invalid = summary("priority")
        invalid["service_tier"]["value"] = "default"
        self.assertIsNone(self._tool_events([], invalid, "priority", request)["provider_observation"]["service_tier"]["value"])
        partial = summary("priority")
        partial["runtime_count"] = 2
        self.assertIsNone(self._tool_events([], partial, "priority", request)["provider_observation"]["service_tier"]["value"])
        mixed = summary("priority")
        mixed["runtime_count"] = mixed["observation_count"] = 2
        mixed["service_tier"] = {"value": "priority", "observed_count": 2,
                                 "distinct_count": 2, "distinct_values": ["default", "priority"]}
        self.assertIsNone(self._tool_events([], mixed, "priority", request)["provider_observation"]["service_tier"]["value"])

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_observation_reaches_final_receipt_without_cli_echo(self):
        from dataclasses import replace
        for prompt, tier in (("ACTUAL_DEFAULT", "default"), ("ACTUAL_PRIORITY", "priority")):
            with self.subTest(tier=tier):
                request = replace(self._request(), prompt=prompt, service_tier="priority")
                result = caller.execute(request)
                self.assertEqual(result["requested_service_tier"], "priority")
                self.assertEqual(result["observed_service_tier"], tier)
                self.assertEqual(result["observed_model"], "gpt-6")
                self.assertEqual(result["usage"], {"total_tokens": 11})

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

    def test_command_completion_needs_an_explicit_zero_exit_code(self):
        for fields in ({}, {"aggregated_output": "not JSON"},
                       {"aggregated_output": json.dumps({"stdout": "ok"})},
                       {"aggregated_output": json.dumps({"exit_code": None})}):
            with self.subTest(fields=fields):
                item = {"type": "command_execution", "status": "completed", **fields}
                self.assertEqual(self._tool_events([item])["tool_loop"],
                                 {"completed_count": 1, "successful_count": 0})

        item = {"type": "command_execution", "status": "completed", "exit_code": 0}
        self.assertEqual(self._tool_events([item])["tool_loop"],
                         {"completed_count": 1, "successful_count": 1})

    def test_nested_terminal_receipt_failure_is_not_a_success(self):
        for receipt in ({"exit_code": 1}, {"outcome": "unknown"},
                        {"terminal_state": "failed"}, {"process_reaped": False},
                        {"process_group_empty": False}, {"termination_proven": False},
                        {"termination_proven": "true"}):
            with self.subTest(receipt=receipt):
                item = {"type": "command_execution", "status": "completed", "command": "cat input.txt",
                        "aggregated_output": json.dumps({"exit_code": 0, "terminal_receipt": receipt})}
                result = self._tool_events([item])
                self.assertEqual(result["tool_loop"], {"completed_count": 1, "successful_count": 0})
                self.assertEqual(result["command_evidence"]["failed_count"], 1)

    def test_command_evidence_is_bounded_and_omits_command_text(self):
        items = [{"type": "command_execution", "status": "completed", "command": f"cat input-{i}.txt",
                  "aggregated_output": json.dumps({"exit_code": 0})}
                 for i in range(core.MAX_COMMAND_EVIDENCE + 1)]
        evidence = self._tool_events(items)["command_evidence"]
        self.assertEqual(evidence["total_count"], core.MAX_COMMAND_EVIDENCE + 1)
        self.assertEqual(evidence["failed_count"], 0)
        self.assertFalse(evidence["complete"])
        self.assertEqual(len(evidence["records"]), core.MAX_COMMAND_EVIDENCE)
        self.assertEqual(evidence["records"][0]["event_index"], 1)
        self.assertNotIn("command", evidence["records"][0])

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
        self.assertEqual(env["TURA_NOKIY_BOUNDED_ONE_TURN"], "1")

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
        self.assertIsInstance(result["phase_timings_ms"], dict)
        run = request.artifact_root / request.request_id
        evidence = json.loads((run / "command-evidence.json").read_text())
        self.assertEqual(evidence, {"total_count": 0, "failed_count": 0,
                                    "complete": True, "records": []})
        self.assertEqual(result["command_evidence_summary"],
                         {"total_count": 0, "failed_count": 0, "complete": True})
        self.assertEqual(result["command_evidence_artifact"]["sha256"],
                         fixtures.file_sha256(run / "command-evidence.json"))
        self.assertFalse(result["result_truncated"])
        self.assertIsNone(result["result_artifact"])
        self.assertFalse((run / "last-message.txt").exists())
        phases = json.loads((run / "supervision.json").read_text())["result"]["phase_timings_ms"]
        self.assertEqual(result["phase_timings_ms"], phases)
        self.assertEqual(set(phases), {"prepare", "session_db_ready", "router_ready",
                                       "cli_launch", "cli_process", "trajectory_parse",
                                       "cleanup", "engine_total"})
        self.assertTrue(all(type(value) is int and value >= 0 for value in phases.values()))
        self.assertLessEqual(abs(sum(value for name, value in phases.items()
                                     if name != "engine_total") - phases["engine_total"]), 7)
        self.assertEqual(caller.execute(request), result)
        self.assertFalse(result["fallback_used"])

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_private_verifier_fd_crosses_supervisor_engine_router_only(self):
        from codex_collaboration_harness.verifier_channel import VerifierChannel
        router = self.f.runtime / "tura_router"
        body = router.read_text()
        body = body.replace("(root/marker).write_text(json.dumps(endpoint))", r'''
import socket
channel=socket.socket(fileno=int(os.environ['NOKIY_VERIFIER_FD']))
channel.sendall((json.dumps({'version':1,'binding':'a'*64,'call_id':'fd-chain','verifier_index':0})+'\n').encode())
reply=json.loads(channel.makefile('rb').readline())
assert reply['result']=={'verified':True}
(root/'channel-readback').write_text(reply['call_id'])
channel.close()
(root/marker).write_text(json.dumps(endpoint))
''')
        router.write_text(body)
        for name in ("tura_exec", "tura_session_db"):
            path = self.f.runtime / name
            path.write_text(path.read_text().replace("name=pathlib", "assert 'NOKIY_VERIFIER_FD' not in os.environ\nname=pathlib")
                            .replace("args=sys.argv", "assert 'NOKIY_VERIFIER_FD' not in os.environ\nargs=sys.argv"))
        self._image()
        request = self._request()
        _, _, capsule, jspace = core.prepare(request)
        run = request.artifact_root / request.request_id
        run.mkdir()
        caller._write_create_only(run / "execution.json", {"request":request.to_wire(include_identity=False)})
        caller._write_create_only(run / "capsule.json", capsule)
        caller._write_create_only(run / "jspace.json", jspace)
        (run / "prompt.txt").write_text(request.prompt)
        fence = "(version 1) (allow default) (deny signal) (allow signal (target same-sandbox))"
        command = ["/usr/bin/sandbox-exec", "-p", fence, sys.executable, "-B", "-m", core.MODULE,
                   "--supervise", str(run / "execution.json"), "--parent-pid", str(os.getpid())]
        calls = []
        with VerifierChannel("a"*64, 1, lambda i,c,e: calls.append(c) or {"verified":True}) as channel:
            env = dict(os.environ, NOKIY_VERIFIER_FD=str(channel.router_fd))
            code, _, failure, _ = caller._run_process(command,cwd=request.workspace,env=env,
                stdin_path=run/"prompt.txt",stdout_path=run/"supervision.json",stderr_path=run/"supervision.stderr",
                timeout=20,output_limit=1048576,pass_fds=(channel.router_fd,),on_spawn=channel.release_router_copy)
        outcome = json.loads((run/"supervision.json").read_text())
        self.assertEqual(code, 0, outcome)
        self.assertIsNone(failure)
        self.assertIsNone(channel.failure)
        self.assertEqual(calls, ["fd-chain"])
        self.assertTrue(outcome["scope"]["no_live_descendants"], outcome)
        self.assertEqual((run/"execution-state/session_log/channel-readback").read_text(), "fd-chain")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_long_result_is_externalized_with_digest_and_cached_replay(self):
        request = self._request(prompt="LONG_RESULT")
        result = caller.execute(request)
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)
        self.assertTrue(result["result_truncated"])
        self.assertEqual(result["request_id"], request.request_id)
        self.assertEqual(result["request_sha256"], request.request_sha256)
        full_text = "long result: " + "\u6e2c\u8a66" * 600
        result_path = request.artifact_root / request.request_id / "last-message.txt"
        self.assertEqual(result_path.read_text(encoding="utf-8"), full_text)
        self.assertEqual(result["result_artifact"], {
            "path": str(result_path),
            "sha256": fixtures.file_sha256(result_path),
            "bytes": len(full_text.encode("utf-8")),
        })
        self.assertEqual(caller.read_terminal(request.artifact_root, request.request_id), result)
        self.assertEqual(caller.execute(request), result)
        from codex_collaboration_harness import result_inspection
        with patch.object(result_inspection.os, "kill", side_effect=ProcessLookupError()):
            inspection = result_inspection.inspect(request.artifact_root, request.request_id, request.native_thread_id)
        self.assertEqual(inspection["status"], "EVIDENCE_VERIFIED", inspection)
        core_path = result_path.parent / "core.jsonl"
        events = [json.loads(line) for line in core_path.read_text().splitlines()]
        events[-1]["model"] = "codex/foreign"
        core_path.write_bytes(b"".join(caller._canonical_bytes(event) + b"\n" for event in events))
        drifted_terminal = dict(result, trajectory_artifact=core._record_artifact(core_path))
        (result_path.parent / "terminal.json").write_bytes(caller._canonical_bytes(drifted_terminal))
        with patch.object(result_inspection.os, "kill", side_effect=ProcessLookupError()):
            rejected = result_inspection.inspect(request.artifact_root, request.request_id, request.native_thread_id)
        self.assertEqual(rejected["first_blocker"], "NOKIY_FULL_CORE_TERMINAL_IDENTITY_MISMATCH")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_source_drift_after_execution_blocks_result(self):
        request = self._request()
        drift = caller.EmbeddedNokiyError("NOKIY_SOURCE_EXCERPT_UNVERIFIED", "changed")
        def verify_source(*args, after_execution=False):
            if after_execution:
                raise drift
        with patch("codex_collaboration_harness.source_excerpt.verify_context_excerpt",
                   side_effect=verify_source) as verify:
            result = caller.execute(request)
        self.assertEqual(verify.call_count, 2)
        self.assertTrue(verify.call_args.kwargs["after_execution"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["first_typed_blocker"], "NOKIY_SOURCE_EXCERPT_UNVERIFIED")
        self.assertTrue(result["cleanup_pass"])

    def test_legacy_context_cannot_skip_excerpt_preflight(self):
        from codex_collaboration_harness.source_excerpt import CONTEXT_MARKER
        capsule = json.loads(self.f.context.read_text())
        capsule["context_summary"] += CONTEXT_MARKER + '{"kind":"edit_preimage"}'
        capsule.pop("semantic_sha256")
        capsule["semantic_sha256"] = fixtures.canonical_sha256(capsule)
        self.f.context.write_text(json.dumps(capsule))
        request = self._request()
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NOKIY_SOURCE_EXCERPT_UNVERIFIED"):
            caller.execute(request)
        self.assertFalse((request.artifact_root / request.request_id).exists())

    def _edit_excerpt_request(self, *, remove_after_edit=False):
        from codex_collaboration_harness import source_excerpt
        source = self.f.workspace / "answer.py"
        source.write_text("def answer():\n    return 1\n")
        contract = json.loads(self.f.jspace.read_text())
        contract.update(read_scopes=["answer.py"], write_scopes=["answer.py"],
                        declared_targets=["answer.py"], source_read=True,
                        allowed_operations=["read", "modify", "command"])
        contract["dcf_generation"]["action_freshness"] = {"source_fingerprints": {"fixture": "test"}}
        self.f.jspace.write_text(json.dumps(contract))
        self.f._canonical_v2_context()
        contract = json.loads(self.f.jspace.read_text())
        authorization = {key: contract.get(key) for key in (
            "repo_root", "matched_surface_ids", "read_scopes", "write_scopes",
            "allowed_operations", "denied_operations", "command_templates", "declared_targets",
            "expansion", "command_effect_policy", "source_read")}
        authorization.update(schema_version="jspace_authorization_v1", required_domain_bindings={})
        contract["authorization_semantic_sha256"] = fixtures.canonical_sha256(authorization)
        contract.pop("content_sha256")
        contract["content_sha256"] = fixtures.canonical_sha256(contract)
        self.f.jspace.write_text(json.dumps(contract))
        excerpt = source_excerpt.extract_edits(self.f.workspace, contract, [{
            "target": "symbol:answer.answer", "path": "answer.py", "line": 1,
            "generation_id": contract["dcf_generation"]["generation_id"]}])
        capsule = json.loads(self.f.context.read_text())
        capsule.update(dcf_generation=contract["dcf_generation"],
                       jspace_semantic_sha256=contract["authorization_semantic_sha256"],
                       context_summary="Edit answer." + source_excerpt.CONTEXT_MARKER + json.dumps(excerpt))
        capsule.pop("semantic_sha256")
        capsule["semantic_sha256"] = fixtures.canonical_sha256(capsule)
        self.f.context.write_text(json.dumps(capsule))
        # This process test stubs DCF freshness only; actual excerpt verification
        # runs in both parent and engine before the synthetic runtime edits.
        python = self.f.workspace / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        self.f._write_executable(python, "#!/bin/sh\nexit 0\n")
        cli = self.f.runtime / "tura_exec"
        effect = ("target.unlink()" if remove_after_edit else
                  "target.write_text('def answer():\\n    return 2\\n')")
        cli.write_text(cli.read_text().replace(
            "target=pathlib.Path(value('-C'),'answer.txt')", "target=pathlib.Path(value('-C'),'answer.py')").replace(
            "target.write_text('full-core result\\n')", effect).replace(
            "jspace['semantic_sha256']", "jspace['authorization_semantic_sha256']"))
        self._image()
        return self._request(), excerpt

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_edit_excerpt_survives_authorized_change_and_terminal_replay(self):
        request, excerpt = self._edit_excerpt_request()
        result = caller.execute(request)
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)
        self.assertTrue(result["cleanup_pass"])
        self.assertEqual(result["source_context_readback"], {
            "kind": "edit_postimage", "mission_acceptance": "parent_owned", "files": [{
                "path": "answer.py", "preimage_sha256": excerpt["files"][0]["source_sha256"],
                "postimage_sha256": fixtures.file_sha256(self.f.workspace / "answer.py"), "changed": True}]})
        self.assertEqual((self.f.workspace / "answer.py").read_text(), "def answer():\n    return 2\n")
        self.assertEqual(caller.execute(request), result)
        self.assertEqual(caller.read_terminal(request.artifact_root, request.request_id), result)

    def test_edit_excerpt_drift_blocks_before_runtime_launch(self):
        request, _ = self._edit_excerpt_request()
        (self.f.workspace / "answer.py").write_text("def answer():\n    return 3\n")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NOKIY_SOURCE_EXCERPT_UNVERIFIED"):
            caller.execute(request)
        self.assertFalse((request.artifact_root / request.request_id).exists())

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_edit_excerpt_missing_postimage_blocks_after_cleanup(self):
        request, _ = self._edit_excerpt_request(remove_after_edit=True)
        result = caller.execute(request)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(result["first_typed_blocker"], "NOKIY_SOURCE_EXCERPT_UNVERIFIED")
        self.assertTrue(result["cleanup_pass"])
        self.assertNotIn("source_context_readback", result)

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
    def test_malformed_file_change_is_blocked_after_effect_and_never_replayed(self):
        request = self._request(prompt="MALFORMED_EDIT_EVENT")
        result = caller.execute(request)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(result["first_typed_blocker"], "FILE_CHANGE_EVENT_INVALID")
        self.assertTrue(result["cleanup_pass"])
        self.assertEqual((self.f.workspace / "answer.txt").read_text(), "full-core result\n")
        with patch.object(caller, "_run_process", side_effect=AssertionError("replayed effect")):
            self.assertEqual(caller.execute(request), result)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_unverified_command_cannot_complete_required_tool_request(self):
        request = self._request(prompt="UNVERIFIED_COMMAND")
        result = caller.execute(request)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(result["first_typed_blocker"], "NOKIY_EMBEDDED_TOOL_LOOP_NOT_OBSERVED")
        self.assertEqual(result["tool_loop"], {"completed_count": 1, "successful_count": 0})
        self.assertTrue(result["cleanup_pass"])
        self.assertFalse((self.f.workspace / "answer.txt").exists())

    @unittest.skipUnless(sys.platform == "darwin", "Darwin process fence")
    def test_timeout_settles_child_that_created_its_own_session(self):
        request = self._request(prompt="TIMEOUT")
        result = caller.execute(request)
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertTrue(result["cleanup_pass"], result)
        self.assertEqual(result["phase_timing_observation"]["request_id"], request.request_id)
        self.assertIsInstance(result["phase_timings_ms"]["cli_launch"], int)
        self.assertIsNone(result["phase_timings_ms"]["cli_process"])
        self.assertIsNone(result["phase_timings_ms"]["engine_total"])
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
            inspection = terminal.pop("result_inspection")
            self.assertEqual(inspection["status"], "INCOMPLETE_EVIDENCE")
            self.assertIsNotNone(inspection["first_blocker"])
            self.assertEqual(inspection["mission_acceptance"], "parent_owned")
            summary_reference = terminal.pop("inspection_summary")
            summary = inspector.load_inspection_summary(
                summary_reference,
                expected_request_id=request.request_id,
                expected_thread_id=request.native_thread_id,
                terminal_reference={
                    "path": str(run / "terminal.json"),
                    "sha256": inspection["terminal_sha256"],
                },
            )
            self.assertEqual(summary["result_inspection"], inspection)
            self.assertEqual(summary["status"], terminal["status"])
            self.assertEqual(summary["first_typed_blocker"], terminal["first_typed_blocker"])
            self.assertEqual(caller.read_terminal(request.artifact_root, request.request_id), terminal)
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
        router = self.f.runtime / "tura_router"
        router.write_text(router.read_text().replace(
            "root=pathlib.Path(os.environ['TURA_HOME'])/'session_log';root.mkdir(exist_ok=True)",
            "(pathlib.Path(os.environ['TURA_HOME'])/'router-started').write_text(str(os.getpid()))\n"
            "sys.exit(23)"))
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


    def test_validated_terminal_delivery_reaches_provider_prompt(self):
        class StopAtPrompt(Exception):
            pass

        render_prompt = core._provider_prompt
        for profile in ("direct", "balanced"):
            for requested_mode in (None, "assistant_reply", "evidence_only"):
                with self.subTest(profile=profile, requested_mode=requested_mode):
                    value = self._request(profile).to_wire(include_identity=False)
                    artifact_root = Path(value["artifact_root"]).parent / (
                        "prompt-" + profile + "-" + (requested_mode or "default"))
                    artifact_root.mkdir()
                    value["artifact_root"] = str(artifact_root)
                    if requested_mode is None:
                        value.pop("terminal_delivery", None)
                    else:
                        value["terminal_delivery"] = requested_mode
                    request = caller.decode_request(value)
                    jspace = json.loads(request.jspace_contract.path.read_text())
                    captured = []

                    def capture_prompt(prompt, grant, terminal_delivery="assistant_reply"):
                        captured.append(render_prompt(prompt, grant,
                                                      terminal_delivery=terminal_delivery))
                        raise StopAtPrompt

                    # Capture the real call site before any provider process starts.
                    with patch.object(core, "prepare", return_value=({"status": "READY"}, None, {}, jspace)), \
                            patch.object(core, "_provider_prompt", side_effect=capture_prompt) as provider_prompt:
                        with self.assertRaises(StopAtPrompt):
                            core.execute_full_core(request)
                    provider_prompt.assert_called_once_with(
                        request.prompt, jspace, terminal_delivery=request.terminal_delivery)
                    self.assertEqual(request.terminal_delivery, requested_mode or "assistant_reply")
                    self.assertEqual(captured, [render_prompt(
                        request.prompt, jspace, terminal_delivery=request.terminal_delivery)])
                    self.assertEqual(core._EVIDENCE_ONLY_COMPLETION_GUIDANCE in captured[0],
                                     requested_mode == "evidence_only")

    def test_evidence_only_prompt_allows_planned_ordered_verification(self):
        guidance = core._EVIDENCE_ONLY_COMPLETION_GUIDANCE
        prompt = core._provider_prompt("caller prompt", {}, terminal_delivery="evidence_only")
        self.assertEqual(prompt, core._capability_gap_prompt("caller prompt" + guidance))
        ordered = ("planned apply_patch step 1", "deterministic verification/readbacks step 2",
                   "task_status done step 3")
        positions = [guidance.index(clause) for clause in ordered]
        self.assertEqual(positions, sorted(positions))
        for clause in (
                "When task instructions permit",
                "semantic decisions are settled",
                "all remaining exact artifacts/effects/checks/readbacks are known",
                "need no result-dependent interpretation",
                "strictly later positive steps, same response",
                "Checks must pass before done executes, not before proposing",
                "Failure/timeout/uncertainty or result-dependent review needs a later model repair/review turn"):
            with self.subTest(clause=clause):
                self.assertIn(clause, guidance)
        self.assertNotIn("required worker verification has passed", guidance)

    def test_evidence_only_prompt_preserves_terminal_guards(self):
        prompt = core._provider_prompt("caller prompt", {}, terminal_delivery="evidence_only")
        for clause in (
                "No prose-only recap",
                "Complete worker work and required checks/readbacks before existing task_status done",
                "Explicitly parent-owned checks are not worker blockers; never claim them passed",
                "Parent acceptance remains required",
                "No done on failed/unknown effects, skipped required checks, or unfinished work",
                "Missing verifiers never transfer required worker verification to the parent or excuse it",
                "For real worker blockers, publish the visible capability-gap handoff before terminal "
                "task_status question/done",
                "do not self-grant tools",
                "Scope/effect/receipt gates still apply"):
            with self.subTest(clause=clause):
                self.assertIn(clause, prompt)

    def test_assistant_reply_prompt_bytes_are_unchanged_by_evidence_only_guidance(self):
        text = "caller prompt\n"
        grants = ({}, json.loads(self._request().jspace_contract.path.read_text()))
        for grant in grants:
            with self.subTest(source_read=grant.get("source_read", False)):
                with patch.object(core, "_EVIDENCE_ONLY_COMPLETION_GUIDANCE", ""):
                    expected = core._provider_prompt(
                        text, grant, terminal_delivery="evidence_only").encode("utf-8")
                self.assertEqual(core._provider_prompt(text, grant).encode("utf-8"), expected)
                self.assertEqual(core._provider_prompt(
                    text, grant, terminal_delivery="assistant_reply").encode("utf-8"), expected)

    def _terminal_evidence_case(self, profile="direct", terminal_status="done"):
        value = self._request(profile, prompt=f"terminal evidence {terminal_status}").to_wire(include_identity=False)
        request = caller.decode_request(dict(value, terminal_delivery="evidence_only"))
        marker = {"type": "nokiy.terminal_evidence", "schema_version": "nokiy_terminal_evidence_v1",
                  "session_id": "full-" + request.request_sha256, "runtime_id": "fixture-runtime",
                  "terminal_status": terminal_status, "delivery_mode": "evidence_only",
                  "parent_acceptance_required": True, "final_summary_turn_executed": False}
        completed = {"type": "turn.completed", "status": "completed", "usage": {"total_tokens": 11},
                     **core._terminal_identity(request)}
        return request, marker, completed

    def _terminal_evidence_records(self, request, events):
        path = self.f.root / request.request_id / "core.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"".join(caller._canonical_bytes(event) + b"\n" for event in events))
        return core._events(path, request.max_trajectory_bytes, request)

    def test_terminal_delivery_environment_is_explicit_and_scrubs_inherited_opt_in(self):
        value = self._request().to_wire(include_identity=False)
        for mode in ("assistant_reply", "evidence_only"):
            request = caller.decode_request(dict(value, terminal_delivery=mode))
            runtime = caller.verify_runtime_image(request.runtime_image, required_artifacts=core.REQUIRED)
            with self.subTest(mode=mode), patch.dict(os.environ, {"TURA_NOKIY_EVIDENCE_ONLY_TERMINAL": "1"}):
                env = core._environment(request, runtime, self.f.root / "state")
            self.assertEqual(env["TURA_NOKIY_BOUNDED_ONE_TURN"], "1")
            if mode == "evidence_only":
                self.assertEqual(env["TURA_NOKIY_EVIDENCE_ONLY_TERMINAL"], "1")
            else:
                self.assertNotIn("TURA_NOKIY_EVIDENCE_ONLY_TERMINAL", env)

    def test_terminal_marker_projects_compact_evidence_not_earlier_planning(self):
        for profile, status in (("direct", "done"), ("balanced", "done"),
                                ("direct", "blocked"), ("balanced", "blocked")):
            request, marker, completed = self._terminal_evidence_case(profile, status)
            events = [{"type": "item.completed", "item": {"type": "command_execution", "status": "completed",
                       "command": "true", "exit_code": 0, "aggregated_output": {"exit_code": 0}}},
                      {"type": "item.completed", "item": {"type": "assistant_message", "text": "Earlier planning. " * 100}},
                      marker, completed]
            with self.subTest(profile=profile, status=status):
                observed = self._terminal_evidence_records(request, events)
                compact = {"terminal_status": status, "delivery_mode": "evidence_only",
                           "parent_acceptance_required": True, "final_summary_turn_executed": False}
                self.assertEqual(observed["final_text"], caller._canonical_bytes(compact).decode())
                self.assertFalse(observed["result_truncated"])
                self.assertIsNone(observed["result_artifact"])
                self.assertEqual(observed["terminal_evidence"], marker)
                self.assertEqual(observed["requested_terminal_delivery"], "evidence_only")
                self.assertEqual(observed["observed_terminal_delivery"], "evidence_only")
                self.assertEqual(observed["usage"], completed["usage"])
                self.assertEqual(observed["command_evidence"], core._command_evidence(events))
                self.assertEqual(observed["tool_loop"]["completed_count"], 1)
                delivery = core._TerminalEvidence(request)
                delivery.observe(marker)
                delivery.validate_projection(observed)

    def test_terminal_marker_absence_falls_back_and_default_has_no_new_fields(self):
        request, _, completed = self._terminal_evidence_case()
        message = {"type": "item.completed", "item": {"type": "assistant_message", "text": "Actual final reply"}}
        observed = self._terminal_evidence_records(request, [message, completed])
        self.assertEqual(observed["final_text"], "Actual final reply")
        self.assertEqual(observed["observed_terminal_delivery"], "assistant_reply")
        self.assertIsNone(observed["terminal_evidence"])
        default = self._request()
        completed.update(core._terminal_identity(default))
        legacy = self._terminal_evidence_records(default, [message, completed])
        self.assertEqual(legacy["final_text"], observed["final_text"])
        for key in core._TerminalEvidence.FIELD_NAMES:
            self.assertNotIn(key, legacy)

    def test_terminal_marker_rejects_malformed_foreign_duplicate_and_late_events(self):
        for status in ("done", "blocked"):
            request, marker, completed = self._terminal_evidence_case(terminal_status=status)
            mutations = [{"schema_version": None}, {"schema_version": "unknown"}, {"extra": 1},
                         {"session_id": "full-foreign"}, {"runtime_id": ""}, {"runtime_id": " "},
                         {"runtime_id": None}, {"runtime_id": 1}, {"terminal_status": "unknown"},
                         {"terminal_status": None}, {"terminal_status": []}, {"terminal_status": {}},
                         {"delivery_mode": "assistant_reply"}, {"parent_acceptance_required": 1},
                         {"final_summary_turn_executed": 0}, {"type": "diagnostic"}]
            cases = [[dict(marker, **change), completed] for change in mutations]
            cases += [[{key: value for key, value in marker.items() if key != "runtime_id"}, completed],
                      [marker, marker, completed]]
            for kind in ("assistant_message", "command_execution", "file_change"):
                for event_type in ("item.started", "item.completed"):
                    cases.append([marker, {"type": event_type, "item": {"type": kind, "text": "late"}}, completed])
            cases.append([marker, {"type": "tool_call"}, completed])
            for index, events in enumerate(cases):
                with self.subTest(status=status, index=index):
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID"):
                        self._terminal_evidence_records(request, events)
            for mode in (None, "assistant_reply"):
                fields = self._request().to_wire(include_identity=False)
                if mode is not None:
                    fields["terminal_delivery"] = mode
                default = caller.decode_request(fields)
                completed.update(core._terminal_identity(default))
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID"):
                    self._terminal_evidence_records(default, [dict(marker, session_id="full-" + default.request_sha256), completed])


if __name__ == "__main__":
    unittest.main()
