# SPDX-License-Identifier: MIT
"""Actual caller subprocess fixtures; no external provider requests."""
from __future__ import annotations
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness.embedded_nokiy import (
    MAX_TERMINAL_BYTES, EmbeddedNokiyError, execute, load_request, preflight, read_terminal,
)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EmbeddedNokiyFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="embedded-nokiy-test-")
        self.root = Path(self.temporary.name).resolve()
        self.workspace, self.artifacts, self.runtime = [self.root / name for name in ("workspace", "artifacts", "runtime")]
        for directory in (self.workspace, self.artifacts, self.runtime):
            directory.mkdir()
        self.previous_codex_home = os.environ.get("CODEX_HOME")
        self.previous_thread_id = os.environ.get("CODEX_THREAD_ID")
        self.native_thread_id = "019fd4f0-1079-77e2-8be7-cbf75ca28df5"  # synthetic fixture, not a real caller
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        (self.codex_home / "auth.json").write_text("{}\n")
        os.environ["CODEX_HOME"] = str(self.codex_home)
        os.environ["CODEX_THREAD_ID"] = self.native_thread_id
        self._write_fake_runtime()
        self.runtime_image = self._write_runtime_image()
        self.context, self.jspace = self._write_context()
        self.codex = self.root / "codex"
        self._write_executable(self.codex, "#!/bin/sh\nexit 0\n")
        self.request_path = self._write_request()

    def tearDown(self) -> None:
        for name, previous in (("CODEX_HOME", self.previous_codex_home), ("CODEX_THREAD_ID", self.previous_thread_id)):
            if previous is None: os.environ.pop(name, None)
            else: os.environ[name] = previous
        self.temporary.cleanup()

    @staticmethod
    def _write_executable(path: Path, source: str) -> None:
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)

    def _write_fake_runtime(self, *, schema: str = "v4") -> None:
        self._write_executable(self.runtime / "tura_router", f"#!{sys.executable}\nexpected_schema = {json.dumps('tura_native_codex_worker_request_' + schema)}\n" + r'''
import hashlib,json,pathlib,sys
assert sys.argv[1] == "native-once"
assert sys.argv[2] == "--worker" and sys.argv[4] == "--worker-sha256"
w=json.load(sys.stdin)
assert w["schema_version"] == expected_schema
assert w["provider_profile"]["model"] == "gpt-6-astra"
assert w["sandbox"] == "read_only"
if expected_schema.endswith("_v4"):
    state_root=pathlib.Path(w["state_root"])
    workspace=pathlib.Path(w["workspace"])
    assert state_root.is_dir() and state_root.resolve(strict=True) == state_root
    assert state_root.name == w["execution_id"] and not state_root.is_relative_to(workspace)
    assert (state_root / "native-request.json").is_file()
else:
    assert expected_schema == "tura_native_codex_worker_request_v3"
    assert "state_root" not in w
assert set(w["command_graph"]["allowed_commands"]) == {"apply_patch","bash","shell_command","zsh"}
with pathlib.Path(__file__).with_suffix(".runs").open("a") as f: f.write(w["execution_id"]+"\n")
instruction=w["task_delta"]["instruction"]
text="x"*5000 if instruction=="LONG" else "bounded result"
result={"schema_version":"tura_native_codex_terminal_envelope_v2",
    **{k:w[k] for k in ["task_id","execution_id","lease_id","execution_profile_sha256","execution_binding_sha256"]},
    "task_context_capsule_sha256":w["expected_task_context_capsule_sha256"],
    "task_delta_sha256":w["expected_task_delta_sha256"],"provider_input_sha256":"a"*64,
    "worker_thread_id":"synthetic-provider-thread","terminal_state":"completed",
    "final_text":text,"final_text_sha256":hashlib.sha256(text.encode()).hexdigest(),
    "tool_observations":[],"event_stream_sha256":"b"*64,"process_exit_code":0,
    "process_reaped":True,"terminal":True,"execution_lease_active":False,
    "ephemeral":True,"private_codex_state_write_count":0,
    "usage":{"input_tokens":120,"cached_input_tokens":80,"output_tokens":7,"total_tokens":127}}
if instruction != "NO_TOOL":
    result["tool_observations"]=[{"item_id":"fixture-tool-1","item_type":"mcp_tool_call",
      "tool_name":"tura_command_graph","effect_id":"fixture-effect-1","input_sha256":"c"*64,
      "output_sha256":"d"*64,"status":"completed","is_error":instruction=="MCP_ERROR"}]
if instruction == "BAD_ID": result["task_id"]="other-task"
if instruction == "FAILED": result["terminal_state"]="failed";result["error"]="fixture turn failed"
if instruction == "UNKNOWN_USAGE": result["usage"]=None
if instruction == "BAD_TEXT": result["final_text"]="different"
if instruction == "INVALID_JSON": print("{broken");sys.exit(0)
print(json.dumps(result))
''')
        for name in ("tura_native_codex_worker", "tura_command_graph"):
            self._write_executable(self.runtime / name, "#!/bin/sh\nexit 0\n")
        (self.runtime / "prompt.md").write_text("fixture immutable balanced instructions\n")

    def _write_runtime_image(self) -> Path:
        paths = {name: self.runtime / name for name in ("tura_router", "tura_native_codex_worker", "tura_command_graph")}
        paths["agent_prompt"] = self.runtime / "prompt.md"
        image = {"schema_version": "tura-dcf-benchmark-v2-repair-runtime-image/v1",
                 "status": "FROZEN_BYTE_EXACT", "runtime_build_identity": "test-build",
                 "native_once_request_schema_version": "tura_native_codex_worker_request_v4",
                 "runtime_root": str(self.runtime), "artifacts": {
                     name: {"path": str(path), "sha256": file_sha256(path), "size": path.stat().st_size}
                     for name, path in paths.items()}}
        path = self.root / "runtime-image.json"
        path.write_text(json.dumps(image, sort_keys=True))
        return path

    def _write_context(self, *, generated_at: datetime | None = None,
                       write_scopes: list[str] | None = None) -> tuple[Path, Path]:
        generation = {"generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
                      "generation_id": "test-generation", "repo_root": str(self.workspace)}
        write_scopes = write_scopes or []
        jspace = {"schema_version": "jspace_contract_v1", "repo_root": str(self.workspace),
                  "write_scopes": write_scopes, "read_scopes": ["**"],
                  "allowed_operations": ["read", "command"] + (["create", "modify"] if write_scopes else []),
                  "denied_operations": ["delete"] if write_scopes else ["create", "modify", "delete"],
                  "dcf_generation": generation, "provenance": {}, "matched_surface_ids": ["fixture"],
                  "focused_verifiers": [], "declared_targets": list(write_scopes),
                  "command_prefixes": ["pwd", "cat input.txt", "cat answer.txt"],
                  "expansion": {"mode": "exact_target_only", "error_code": "JSPACE_EXPANSION_REQUIRED", "mutation_on_expansion": False}}
        jspace["semantic_sha256"] = canonical_sha256(jspace)
        context = {"schema_version": "task_context_capsule_v1", "dcf_generation": generation,
                   "jspace_semantic_sha256": jspace["semantic_sha256"],
                   "mission": {"mission_id": "fixture-mission", "task_id": "fixture-task", "mode": "DELIVERY",
                               "objective": "Repair one bounded fixture", "current_predicate": "fixture_false"},
                   "context_summary": "Bounded fixture source evidence only.",
                   "surface": {"repo_root": str(self.workspace), "matched_surface_ids": ["fixture"], "declared_targets": list(write_scopes)},
                   "authority": {"forbidden_effects": ["live", "broker"]},
                   "evidence_refs": [{"id": "fixture-source", "kind": "source", "sha256": "a"*64}],
                   "focused_verifiers": [{"verifier_id": "fixture"}]}
        context["semantic_sha256"] = canonical_sha256(context)
        context_path, jspace_path = self.root / "context.json", self.root / "jspace.json"
        context_path.write_text(json.dumps(context, sort_keys=True))
        jspace_path.write_text(json.dumps(jspace, sort_keys=True))
        return context_path, jspace_path

    def _write_request(self, *, prompt: str = "return a result", require_tool_call: bool = True) -> Path:
        request = {"schema_version": "tura_embedded_request_v3",
                   "runtime_image": {"path": str(self.runtime_image), "sha256": file_sha256(self.runtime_image)},
                   "codex": {"path": str(self.codex), "sha256": file_sha256(self.codex)},
                   "workspace": str(self.workspace), "artifact_root": str(self.artifacts),
                   "context_capsule": {"path": str(self.context), "sha256": file_sha256(self.context)},
                   "jspace_contract": {"path": str(self.jspace), "sha256": file_sha256(self.jspace)},
                   "prompt": prompt, "model": "gpt-6-astra", "native_thread_id": self.native_thread_id,
                   "persistence_mode": "native_codex_thread_only", "reasoning_effort": "high",
                   "require_tool_call": require_tool_call, "timeout_seconds": 10,
                   "max_context_age_seconds": 300, "max_trajectory_bytes": 64*1024, "max_result_bytes": 512,
                   "model_acceleration": False, "allow_provider_network": True, "authority_effect": "none"}
        path = self.root / ("request-" + hashlib.sha256(prompt.encode()).hexdigest()[:12] + ".json")
        path.write_text(json.dumps(request, sort_keys=True))
        return path

    def test_unmarked_request_wire_and_identity_remain_canonical(self) -> None:
        raw = json.loads(self.request_path.read_text())
        raw.update(model="gpt-6-sol", reasoning_effort="max", execution_profile="direct")
        self.request_path.write_text(json.dumps(raw))
        request = load_request(self.request_path)
        digest = canonical_sha256(raw)
        self.assertEqual(request.to_wire(include_identity=False), raw)
        self.assertNotIn("model_selection", request.to_wire())
        self.assertEqual(request.request_sha256, digest)
        self.assertEqual(request.request_id, "tura_embedded_" + digest)

    def test_preflight_execute_and_cached_replay(self) -> None:
        request = load_request(self.request_path)
        self.assertEqual(preflight(request)["status"], "READY")
        terminal = execute(request)
        self.assertEqual(terminal["status"], "RESULT_AVAILABLE", terminal)
        self.assertEqual(terminal["result_text"], "bounded result")
        self.assertEqual(terminal["usage"]["total_tokens"], 127)
        self.assertEqual(terminal["tool_loop"]["successful_count"], 1)
        self.assertEqual(terminal["tool_loop"]["item_types"], ["mcp_tool_call"])
        self.assertIsNone(terminal["tool_loop"]["started_count"])
        self.assertEqual(terminal["native_thread_id"], self.native_thread_id)
        self.assertEqual(terminal["durable_session_owner"], "native_codex_thread")
        self.assertTrue(terminal["cleanup_pass"])
        self.assertFalse((self.workspace / ".tura").exists())
        self.assertNotIn("state_manifest", terminal)
        receipt = self.artifacts / request.request_id / "terminal.json"
        self.assertLessEqual(receipt.stat().st_size, MAX_TERMINAL_BYTES)
        trajectory = Path(terminal["trajectory_artifact"]["path"])
        before = trajectory.stat().st_mtime_ns
        time.sleep(0.01)
        self.assertEqual(execute(request), terminal)
        self.assertEqual(trajectory.stat().st_mtime_ns, before)
        self.context.unlink()
        self.assertEqual(read_terminal(self.artifacts, request.request_id), terminal)
        self.assertEqual(len((self.runtime / "tura_router.runs").read_text().splitlines()), 1)

    def test_nullable_full_core_lifetime_and_finite_identity_roundtrip(self) -> None:
        from codex_collaboration_harness.embedded_nokiy import decode_request
        raw = json.loads(self.request_path.read_text())
        for profile in ("direct", "balanced"):
            for timeout in (None, 10, 900):
                with self.subTest(profile=profile, timeout=timeout):
                    value = dict(raw, execution_profile=profile, timeout_seconds=timeout)
                    request = decode_request(value)
                    self.assertEqual(request.timeout_seconds, timeout)
                    self.assertEqual(request.to_wire(include_identity=False), value)
                    self.assertEqual(request.request_sha256, canonical_sha256(value))
                    self.assertEqual(decode_request(request.to_wire(include_identity=False)), request)

    def test_nullable_trajectory_is_v3_full_core_only_and_keeps_identity(self) -> None:
        from codex_collaboration_harness import embedded_nokiy as caller
        raw = json.loads(self.request_path.read_text())
        for profile in ("direct", "balanced"):
            for limit in (None, 1024, caller.MAX_TRAJECTORY_BYTES):
                with self.subTest(profile=profile, limit=limit):
                    value = dict(raw, execution_profile=profile, max_trajectory_bytes=limit)
                    request = caller.decode_request(value)
                    self.assertEqual(request.max_trajectory_bytes, limit)
                    self.assertEqual(request.to_wire(include_identity=False), value)
                    self.assertEqual(request.request_sha256, canonical_sha256(value))
                    self.assertEqual(caller.decode_request(request.to_wire(include_identity=False)), request)
        for schema, keys in ((caller.PREVIOUS_REQUEST_SCHEMA_VERSION, caller.PREVIOUS_REQUEST_KEYS),
                             (caller.LEGACY_REQUEST_SCHEMA_VERSION, caller.LEGACY_REQUEST_KEYS)):
            value = {key: raw.get(key) for key in keys}
            value.update(schema_version=schema, max_trajectory_bytes=None)
            with self.subTest(schema=schema), patch.object(caller, "_plain_path", side_effect=AssertionError("path inspected")):
                with self.assertRaisesRegex(EmbeddedNokiyError, "max_trajectory_bytes=null requires v3 full_core"):
                    caller.decode_request(value)
        with patch.object(caller, "_plain_path", side_effect=AssertionError("path inspected")):
            with self.assertRaisesRegex(EmbeddedNokiyError, "max_trajectory_bytes=null requires v3 full_core"):
                caller.decode_request(dict(raw, max_trajectory_bytes=None))
        for limit in (True, False, 1023, caller.MAX_TRAJECTORY_BYTES + 1, 1024.0, "1024"):
            with self.subTest(limit=limit), patch.object(caller, "_plain_path", side_effect=AssertionError("path inspected")):
                with self.assertRaisesRegex(EmbeddedNokiyError, "REQUEST_INVALID"):
                    caller.decode_request(dict(raw, execution_profile="direct", max_trajectory_bytes=limit))
        request = replace(load_request(self.request_path), max_trajectory_bytes=None)
        with patch.object(caller, "_verify_native_thread_binding", side_effect=AssertionError("native inspected")), \
                patch.object(caller, "verify_runtime_image", side_effect=AssertionError("image inspected")):
            for operation in (preflight, execute):
                with self.assertRaisesRegex(EmbeddedNokiyError, "native_once requires a finite max_trajectory_bytes"):
                    operation(request)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_native_null_and_invalid_finite_lifetimes_rejected_before_effects(self) -> None:
        from codex_collaboration_harness import embedded_nokiy as caller
        raw = json.loads(self.request_path.read_text())
        for timeout in (None, True, False, 9, 901, 10.0, "900", float("inf")):
            with self.subTest(timeout=timeout), \
                    patch.object(caller, "_plain_path", side_effect=AssertionError("path inspected")):
                with self.assertRaisesRegex(EmbeddedNokiyError, "REQUEST_INVALID"):
                    caller.decode_request(dict(raw, timeout_seconds=timeout))
        request = replace(load_request(self.request_path), timeout_seconds=None)
        with patch.object(caller, "_verify_native_thread_binding", side_effect=AssertionError("native inspected")), \
                patch.object(caller, "verify_runtime_image", side_effect=AssertionError("image inspected")):
            for operation in (preflight, execute):
                with self.assertRaisesRegex(EmbeddedNokiyError, "native_once requires a finite"):
                    operation(request)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_optional_process_timeout_preserves_cancellation_limits_and_cleanup(self) -> None:
        from codex_collaboration_harness import embedded_nokiy as caller
        cases = (("complete", None, None), ("deadline", 10, "NOKIY_EMBEDDED_RUNTIME_TIMEOUT"),
                 ("cancel", None, "NOKIY_EMBEDDED_CANCELLED"),
                 ("output", None, "NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED"))
        for name, timeout, expected in cases:
            with self.subTest(name=name), caller._cancellation_signals() as cancelled:
                process = Mock()
                process.poll.side_effect = [None, None, 0]
                process.wait.return_value = 0
                def spawn(*args, **kwargs):
                    if name == "output":
                        kwargs["stdout"].write(b"xx")
                        kwargs["stdout"].flush()
                    return process
                def sleep(_seconds):
                    if name == "cancel":
                        cancelled.append(1)
                with patch.object(caller.subprocess, "Popen", side_effect=spawn), \
                        patch.object(caller.time, "monotonic", side_effect=[0, 10000, 10001]), \
                        patch.object(caller.time, "sleep", side_effect=sleep), \
                        patch.object(caller, "_stop_process_group", return_value={"clean": True}) as stop:
                    code, wall, failure, cleanup = caller._run_process(
                        ["unused"], cwd=self.workspace, env={}, stdin_path=self.request_path,
                        stdout_path=self.root / (name + ".out"), stderr_path=self.root / (name + ".err"),
                        timeout=timeout, output_limit=1)
                self.assertEqual((code, failure, cleanup), (0, expected, {"clean": True}))
                self.assertGreaterEqual(wall, 10000)
                stop.assert_called_once_with(process, graceful_seconds=0)

    def test_native_state_root_is_request_bound_and_outside_workspace(self) -> None:
        from codex_collaboration_harness.embedded_nokiy import _prepare
        request = load_request(self.request_path)
        wire = _prepare(request)[2]
        self.assertEqual(wire["state_root"], str(self.artifacts / request.request_id))
        self.assertEqual(wire["schema_version"], "tura_native_codex_worker_request_v4")
        self.assertEqual(wire["execution_binding_sha256"], canonical_sha256({
            key: value for key, value in wire.items() if key != "execution_binding_sha256"}))
        payload = json.loads(self.request_path.read_text())
        payload["artifact_root"] = str(self.workspace)
        self.request_path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(EmbeddedNokiyError, "NOKIY_EMBEDDED_REQUEST_INVALID"):
            preflight(load_request(self.request_path))
        self.assertFalse((self.workspace / ".tura").exists())

    def _set_native_once_schema(self, declaration: object = ...) -> None:
        image = json.loads(self.runtime_image.read_text())
        image.pop("native_once_request_schema_version", None)
        if declaration is not ...:
            image["native_once_request_schema_version"] = declaration
        self.runtime_image.write_text(json.dumps(image, sort_keys=True))
        payload = json.loads(self.request_path.read_text())
        payload["runtime_image"]["sha256"] = file_sha256(self.runtime_image)
        self.request_path.write_text(json.dumps(payload, sort_keys=True))

    def test_native_once_v3_image_never_receives_v4_state_root(self) -> None:
        from codex_collaboration_harness.embedded_nokiy import _prepare
        self._write_fake_runtime(schema="v3")
        # Reseal the immutable executable identity after selecting a strict v3 router.
        image = json.loads(self.runtime_image.read_text())
        image["artifacts"]["tura_router"]["sha256"] = file_sha256(self.runtime / "tura_router")
        image["artifacts"]["tura_router"]["size"] = (self.runtime / "tura_router").stat().st_size
        image["runtime_build_identity"] = "v4-sounding-label-is-not-a-capability"
        self.runtime_image.write_text(json.dumps(image, sort_keys=True))
        self._set_native_once_schema()
        request = load_request(self.request_path)
        wire = _prepare(request)[2]
        self.assertEqual(wire["schema_version"], "tura_native_codex_worker_request_v3")
        self.assertNotIn("state_root", wire)
        self.assertEqual(wire["execution_binding_sha256"], canonical_sha256({
            key: value for key, value in wire.items() if key != "execution_binding_sha256"}))
        result = execute(request)
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)
        self.assertEqual(result["usage"]["total_tokens"], 127)
        self.assertEqual(result["tool_loop"]["successful_count"], 1)
        self.assertFalse((self.workspace / ".tura").exists())
        self.assertEqual(len((self.runtime / "tura_router.runs").read_text().splitlines()), 1)

    def test_native_once_explicit_v3_declaration_is_accepted(self) -> None:
        from codex_collaboration_harness.embedded_nokiy import _prepare
        self._set_native_once_schema("tura_native_codex_worker_request_v3")
        wire = _prepare(load_request(self.request_path))[2]
        self.assertEqual(wire["schema_version"], "tura_native_codex_worker_request_v3")
        self.assertNotIn("state_root", wire)

    def test_native_once_unknown_or_malformed_capability_fails_before_provider(self) -> None:
        from codex_collaboration_harness import embedded_nokiy
        for declaration in (None, False, 4, [], {}, "v4", "tura_native_codex_worker_request_v5"):
            with self.subTest(declaration=declaration):
                self._set_native_once_schema(declaration)
                with patch.object(embedded_nokiy, "_run_process", side_effect=AssertionError("provider invoked")):
                    with self.assertRaisesRegex(EmbeddedNokiyError, "RUNTIME_IMAGE_INVALID"):
                        execute(load_request(self.request_path))
                self.assertFalse((self.artifacts / load_request(self.request_path).request_id).exists())

    def test_direct_dispatch_does_not_require_native_once_worker_artifacts(self) -> None:
        from codex_collaboration_harness import embedded_nokiy
        image = json.loads(self.runtime_image.read_text())
        for name in ("tura_native_codex_worker", "tura_router", "tura_command_graph"):
            image["artifacts"].pop(name)
        self.runtime_image.write_text(json.dumps(image, sort_keys=True))
        payload = json.loads(self.request_path.read_text())
        payload["runtime_image"]["sha256"] = file_sha256(self.runtime_image)
        payload["execution_profile"] = "direct"
        self.request_path.write_text(json.dumps(payload, sort_keys=True))
        request = load_request(self.request_path)
        ready, terminal = {"status": "READY"}, {"status": "RESULT_AVAILABLE"}
        with patch.object(embedded_nokiy, "_prepare", side_effect=AssertionError("native-once preflight")), \
             patch("codex_collaboration_harness.full_core.prepare", return_value=(ready,)) as direct_prepare, \
             patch("codex_collaboration_harness.full_core.execute_full_core", return_value=terminal) as direct_execute:
            self.assertIs(preflight(request), ready)
            self.assertIs(execute(request), terminal)
        direct_prepare.assert_called_once_with(request)
        direct_execute.assert_called_once_with(request)
        self.assertFalse((self.artifacts / request.request_id).exists())

    def test_usage_is_preserved_without_synthetic_zero(self) -> None:
        result = execute(load_request(self._write_request(prompt="UNKNOWN_USAGE")))
        self.assertIsNone(result["usage"])
        self.assertEqual(result["status"], "RESULT_AVAILABLE")

    def test_omitted_profile_defaults_to_sol_max_default(self) -> None:
        from codex_collaboration_harness.embedded_nokiy import _prepare
        payload = json.loads(self.request_path.read_text())
        for key in ("model", "reasoning_effort", "model_acceleration"):
            payload.pop(key)
        self.request_path.write_text(json.dumps(payload))
        request = load_request(self.request_path)
        ready, _, wire = _prepare(request)
        self.assertEqual(wire["provider_profile"], {
            "model": "gpt-6.1-sol", "reasoning_effort": "max", "service_tier": "default"})
        self.assertEqual(request.to_wire()["service_tier"], "default")
        self.assertEqual(ready["requested_service_tier"], "default")
        self.assertIsNone(ready["observed_service_tier"])

    def test_explicit_profile_wins_without_legacy_request_identity_drift(self) -> None:
        from codex_collaboration_harness.embedded_nokiy import _prepare
        original = json.loads(self.request_path.read_text())
        for acceleration, expected_tier in ((False, "default"), (True, "priority")):
            payload = {**original, "model_acceleration": acceleration}
            self.request_path.write_text(json.dumps(payload))
            request = load_request(self.request_path)
            self.assertEqual(request.to_wire(include_identity=False), payload)
            self.assertEqual(request.request_sha256, canonical_sha256(payload))
            self.assertEqual(_prepare(request)[2]["provider_profile"]["service_tier"], expected_tier)
        for tier in ("default", "priority", "ultrafast"):
            payload = {**original, "model_acceleration": True, "service_tier": tier}
            self.request_path.write_text(json.dumps(payload))
            ready, _, wire = _prepare(load_request(self.request_path))
            self.assertEqual(wire["provider_profile"], {
                "model": "gpt-6-astra", "reasoning_effort": "high", "service_tier": tier})
            self.assertEqual(ready["requested_service_tier"], tier)
        for invalid in (None, "fast", "unknown", []):
            self.request_path.write_text(json.dumps({**original, "service_tier": invalid}))
            with self.assertRaisesRegex(EmbeddedNokiyError, "REQUEST_INVALID"):
                load_request(self.request_path)

    def test_provider_selection_is_bound_and_authentication_is_native_owned(self) -> None:
        from codex_collaboration_harness.embedded_nokiy import _prepare
        original = load_request(self.request_path)
        _, _, original_wire = _prepare(original)
        self.assertNotIn("model_provider", original.to_wire())
        payload = json.loads(self.request_path.read_text())
        payload["model_provider"] = "fixture-provider"
        self.request_path.write_text(json.dumps(payload))
        (self.codex_home / "auth.json").unlink()
        selected = load_request(self.request_path)
        ready, _, selected_wire = _prepare(selected)
        self.assertNotEqual(original.request_id, selected.request_id)
        self.assertNotEqual(original_wire["execution_profile_sha256"], selected_wire["execution_profile_sha256"])
        self.assertNotEqual(original_wire["execution_binding_sha256"], selected_wire["execution_binding_sha256"])
        self.assertEqual(selected_wire["provider_profile"]["model_provider"], "fixture-provider")
        self.assertEqual(ready["requested_model_provider"], "fixture-provider")
        self.assertIsNone(ready["observed_model_provider"])
        payload["model_provider"] = "invalid provider"
        self.request_path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(EmbeddedNokiyError, "REQUEST_INVALID"):
            load_request(self.request_path)

    def test_long_result_is_externalized_and_preview_is_bounded(self) -> None:
        result = execute(load_request(self._write_request(prompt="LONG")))
        self.assertTrue(result["result_truncated"])
        self.assertLessEqual(len(result["result_text"].encode()), 512)
        self.assertEqual(result["result_artifact"]["bytes"], 5000)

    def test_required_tool_call_fails_closed_when_not_observed(self) -> None:
        result = execute(load_request(self._write_request(prompt="NO_TOOL")))
        self.assertEqual(result["first_typed_blocker"], "NOKIY_EMBEDDED_TOOL_LOOP_NOT_OBSERVED")
        self.assertFalse(result["tool_loop"]["observed"])

    def test_reasoning_only_request_does_not_require_tool_call(self) -> None:
        result = execute(load_request(self._write_request(prompt="NO_TOOL", require_tool_call=False)))
        self.assertEqual(result["status"], "RESULT_AVAILABLE", result)

    def test_legacy_request_decoding_is_readback_only(self) -> None:
        for version in ("v1", "v2"):
            path = self._write_request(prompt="NO_TOOL")
            payload = json.loads(path.read_text())
            payload["schema_version"] = f"tura_embedded_request_{version}"
            payload.pop("native_thread_id"); payload.pop("persistence_mode")
            if version == "v1": payload.pop("require_tool_call")
            path.write_text(json.dumps(payload, sort_keys=True))
            request = load_request(path)
            self.assertEqual(request.require_tool_call, version == "v2")
            with self.assertRaisesRegex(EmbeddedNokiyError, "LEGACY_REQUEST_READBACK_ONLY"):
                preflight(request)

    def test_native_thread_mismatch_fails_before_execution(self) -> None:
        payload = json.loads(self.request_path.read_text())
        payload["native_thread_id"] = "11111111-1111-1111-1111-111111111111"
        self.request_path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(EmbeddedNokiyError, "NATIVE_THREAD_MISMATCH"):
            preflight(load_request(self.request_path))

    def _canonical_v2_context(self, policy: object = "trusted_argv_effects_v1", *, include_policy: bool = True) -> None:
        contract = json.loads(self.jspace.read_text())
        contract.pop("semantic_sha256")
        contract.pop("command_prefixes")
        contract["schema_version"] = "jspace_contract_v2"
        contract["command_templates"] = [{"argv": ["pwd"], "effects": ["read"], "targets": []}]
        contract["dcf_generation"]["required_domain_bindings"] = {}
        authorization = {key: contract.get(key) for key in (
            "repo_root", "matched_surface_ids", "read_scopes", "write_scopes",
            "allowed_operations", "denied_operations", "command_templates", "declared_targets", "expansion",
        )}
        authorization.update(schema_version="jspace_authorization_v1", required_domain_bindings={})
        if include_policy:
            contract["command_effect_policy"] = policy
            authorization["command_effect_policy"] = policy
        contract["authorization_semantic_sha256"] = canonical_sha256(authorization)
        contract["content_sha256"] = canonical_sha256(contract)
        self.jspace.write_text(json.dumps(contract))
        context = json.loads(self.context.read_text())
        context.pop("semantic_sha256")
        context["jspace_semantic_sha256"] = contract["authorization_semantic_sha256"]
        context["semantic_sha256"] = canonical_sha256(context)
        self.context.write_text(json.dumps(context))

    def test_canonical_v2_policy_authorization_is_accepted(self) -> None:
        self._canonical_v2_context()
        self.assertEqual(preflight(load_request(self._write_request()))["status"], "READY")

    def test_legacy_v2_identity_remains_accepted(self) -> None:
        self._canonical_v2_context(include_policy=False)
        self.assertEqual(preflight(load_request(self._write_request()))["status"], "READY")

    def test_v2_unknown_policy_rejected_even_when_resealed(self) -> None:
        for policy in ("unrestricted", None, {}, 1):
            with self.subTest(policy=policy):
                self.context, self.jspace = self._write_context()
                self._canonical_v2_context(policy)
                with self.assertRaisesRegex(EmbeddedNokiyError, "unsupported command effect policy"):
                    preflight(load_request(self._write_request()))

    def test_v2_policy_cannot_be_removed_by_resealing_content_only(self) -> None:
        self._canonical_v2_context()
        contract = json.loads(self.jspace.read_text())
        contract.pop("command_effect_policy")
        contract.pop("content_sha256")
        contract["content_sha256"] = canonical_sha256(contract)
        self.jspace.write_text(json.dumps(contract))
        with self.assertRaisesRegex(EmbeddedNokiyError, "semantic digest differs"):
            preflight(load_request(self._write_request()))

    def test_runtime_image_drift_fails_closed(self) -> None:
        request = load_request(self.request_path)
        (self.runtime / "tura_router").write_text("drift")
        with self.assertRaisesRegex(EmbeddedNokiyError, "RUNTIME_IMAGE_DRIFT"):
            preflight(request)

    def test_stale_context_fails_closed(self) -> None:
        self.context, self.jspace = self._write_context(generated_at=datetime.now(timezone.utc)-timedelta(hours=1))
        with self.assertRaisesRegex(EmbeddedNokiyError, "CONTEXT_STALE"):
            preflight(load_request(self._write_request()))

    def test_no_effect_request_rejects_write_scope(self) -> None:
        self.context, self.jspace = self._write_context(write_scopes=["answer.txt"])
        with self.assertRaisesRegex(EmbeddedNokiyError, "AUTHORITY_MISMATCH"):
            preflight(load_request(self._write_request()))

    def test_incomplete_prior_attempt_is_not_retried(self) -> None:
        request = load_request(self.request_path)
        (self.artifacts / request.request_id).mkdir()
        with self.assertRaisesRegex(EmbeddedNokiyError, "UNCERTAIN_PRIOR_ATTEMPT"):
            execute(request)

    def test_bad_terminal_identity_text_and_json_cannot_complete(self) -> None:
        for prompt in ("BAD_ID", "BAD_TEXT", "INVALID_JSON", "FAILED"):
            result = execute(load_request(self._write_request(prompt=prompt)))
            self.assertEqual(result["status"], "BLOCKED", result)
            self.assertIsNotNone(result["first_typed_blocker"])
            self.assertFalse(result["fallback_used"])

    def test_mcp_transport_success_is_not_semantic_success(self) -> None:
        result = execute(load_request(self._write_request(prompt="MCP_ERROR")))
        self.assertEqual(result["tool_loop"]["completed_count"], 1)
        self.assertEqual(result["tool_loop"]["successful_count"], 0)
        self.assertEqual(result["status"], "BLOCKED")


    def test_terminal_delivery_default_keeps_wire_and_hash_and_opt_in_binds_identity(self):
        from codex_collaboration_harness import embedded_nokiy as caller
        raw = json.loads(self.request_path.read_text())
        for profile in ("direct", "balanced"):
            value = dict(raw, execution_profile=profile)
            baseline = caller.decode_request(value)
            explicit_default = caller.decode_request(dict(value, terminal_delivery="assistant_reply"))
            self.assertEqual(baseline.to_wire(), explicit_default.to_wire())
            self.assertNotIn("terminal_delivery", baseline.to_wire())
            self.assertEqual(baseline.request_sha256, canonical_sha256(value))
            opted_value = dict(value, terminal_delivery="evidence_only")
            opted = caller.decode_request(opted_value)
            self.assertEqual(opted.terminal_delivery, "evidence_only")
            self.assertEqual(opted.to_wire(include_identity=False), opted_value)
            self.assertEqual(opted.request_sha256, canonical_sha256(opted_value))
            self.assertNotEqual(opted.request_sha256, baseline.request_sha256)
            self.assertEqual(caller.decode_request(opted.to_wire(include_identity=False)), opted)

    def test_terminal_delivery_rejects_null_unknown_and_unsupported_ingress_before_paths(self):
        from codex_collaboration_harness import embedded_nokiy as caller
        raw = json.loads(self.request_path.read_text())
        invalid = [dict(raw, execution_profile=profile, terminal_delivery=mode)
                   for profile in ("direct", "balanced")
                   for mode in (None, "", "unknown", True, 1, {}, [])]
        invalid += [dict(raw, terminal_delivery=mode) for mode in ("assistant_reply", "evidence_only")]
        invalid += [dict(raw, schema_version=schema, execution_profile="direct", terminal_delivery=mode)
                    for schema in (caller.LEGACY_REQUEST_SCHEMA_VERSION, caller.PREVIOUS_REQUEST_SCHEMA_VERSION)
                    for mode in ("assistant_reply", "evidence_only")]
        for value in invalid:
            with self.subTest(value=value), patch.object(caller, "_plain_path", side_effect=AssertionError("path inspected")):
                with self.assertRaises(EmbeddedNokiyError):
                    caller.decode_request(value)

    def test_create_only_summary_writer_uses_pinned_directory_and_never_overwrites(self):
        from codex_collaboration_harness import embedded_nokiy as caller
        descriptor = os.open(self.artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            name = Path("inspection-summary.json")
            caller._write_create_only(name, {"historical": True}, dir_fd=descriptor)
            raw = (self.artifacts / name).read_bytes()
            self.assertEqual(raw, caller._canonical_bytes({"historical": True}) + b"\n")
            with self.assertRaises(FileExistsError):
                caller._write_create_only(name, {"historical": False}, dir_fd=descriptor)
            self.assertEqual((self.artifacts / name).read_bytes(), raw)
        finally:
            os.close(descriptor)


class PreparationFailureDiagnosticsTests(unittest.TestCase):
    secret = "NOKIY_FAILURE_DIAGNOSTIC_SECRET_SENTINEL"

    def _prepare_error(self, error):
        output = io.StringIO()
        with patch("codex_collaboration_harness.full_stack.prepare_request", side_effect=error) as prepare, \
                patch("sys.stdout", output), patch.object(
                    caller, "execute", side_effect=AssertionError("model execution forbidden")) as execute:
            code = caller.main(["prepare", "--request", "unused-draft.json",
                                "--action", "unused-action.json", "--output-dir", "unused-output"])
        prepare.assert_called_once()
        execute.assert_not_called()
        self.assertEqual(code, 2)
        self.assertNotIn(self.secret, output.getvalue())
        return json.loads(output.getvalue())

    def test_old_error_contract_remains_digest_only(self):
        error = EmbeddedNokiyError("NOKIY_LOCAL_CONTEXT_INVALID", self.secret)
        self.assertIsNone(error.issues)
        self.assertEqual(str(error), "NOKIY_LOCAL_CONTEXT_INVALID: " + self.secret)
        self.assertEqual(self._prepare_error(error), {
            "schema_version": caller.FAILURE_SCHEMA_VERSION,
            "status": "BLOCKED_PREEXECUTION",
            "first_typed_blocker": error.code,
            "detail_sha256": hashlib.sha256(self.secret.encode()).hexdigest(),
            "authority_effect": "none",
            "native_codex_control_plane_mutation_count": 0,
            "fallback_used": False,
        })

    def test_cli_preserves_only_static_bounded_issues(self):
        issues = [
            {"path": "action.read_scopes", "reason": "input_missing"},
            {"path": "action.command_templates", "reason": "invalid_shape"},
            {"path": "action.command_templates", "reason": "unsupported_command"},
            {"path": "action.command_templates", "reason": "invalid_bindings"},
        ] * 2
        error = EmbeddedNokiyError("NOKIY_LOCAL_CONTEXT_INVALID", self.secret, issues=issues)
        result = self._prepare_error(error)
        self.assertEqual(result["issues"], issues)
        self.assertEqual(result["detail_sha256"], hashlib.sha256(self.secret.encode()).hexdigest())
        self.assertEqual(result["status"], "BLOCKED_PREEXECUTION")
        self.assertEqual(result["authority_effect"], "none")
        self.assertIs(result["fallback_used"], False)

    def test_invalid_issue_metadata_is_omitted_as_a_whole(self):
        safe = {"path": "action.read_scopes", "reason": "input_missing"}
        invalid = (
            None, [], {}, (safe,), [safe] * 9, [None], [self.secret],
            [{"path": "action.read_scopes"}],
            [{**safe, "detail": self.secret}],
            [{"path": self.secret, "reason": "input_missing"}],
            [{"path": "action.read_scopes", "reason": self.secret}],
            [{"path": "action.read_scopes", "reason": "invalid_shape"}],
            [{"path": "action.command_templates[0]", "reason": "invalid_shape"}],
            [{"path": [], "reason": "input_missing"}],
            [{"path": "action.read_scopes", "reason": False}],
            [{"path": self.secret * 1024, "reason": "input_missing"}],
            [safe, {"path": "action.read_scopes", "reason": self.secret}],
        )
        for issues in invalid:
            with self.subTest(issues=issues):
                result = self._prepare_error(EmbeddedNokiyError(
                    "NOKIY_LOCAL_CONTEXT_INVALID", self.secret, issues=issues))
                self.assertNotIn("issues", result)
                self.assertEqual(result["detail_sha256"], hashlib.sha256(self.secret.encode()).hexdigest())

    def test_unknown_and_raw_errors_remain_digest_only(self):
        errors = (
            EmbeddedNokiyError("NOKIY_UNKNOWN_FAILURE", self.secret,
                              issues=[{"path": "action.read_scopes", "reason": "input_missing"}]),
            OSError(self.secret), ValueError(self.secret),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                result = self._prepare_error(error)
                self.assertNotIn("issues", result)
                self.assertEqual(result["first_typed_blocker"],
                                 error.code if isinstance(error, EmbeddedNokiyError)
                                 else "NOKIY_EMBEDDED_INPUT_ERROR")
                self.assertEqual(result["detail_sha256"], hashlib.sha256(self.secret.encode()).hexdigest())

    def test_successful_prepare_cli_does_not_use_failure_diagnostics(self):
        output = io.StringIO()
        with patch("codex_collaboration_harness.full_stack.prepare_request",
                   return_value={"status": "PREPARED"}) as prepare, patch("sys.stdout", output):
            code = caller.main(["prepare", "--request", "unused-draft.json",
                                "--action", "unused-action.json", "--output-dir", "unused-output"])
        prepare.assert_called_once()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"status": "PREPARED"})


if __name__ == "__main__":
    unittest.main()
