# SPDX-License-Identifier: MIT
"""Actual caller subprocess fixtures; no external provider requests."""
from __future__ import annotations
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

    def _write_fake_runtime(self) -> None:
        self._write_executable(self.runtime / "tura_router", f"#!{sys.executable}\n" + r'''
import hashlib,json,pathlib,sys
assert sys.argv[1] == "native-once"
assert sys.argv[2] == "--worker" and sys.argv[4] == "--worker-sha256"
w=json.load(sys.stdin)
assert w["schema_version"] == "tura_native_codex_worker_request_v3"
assert w["provider_profile"]["model"] == "gpt-6-astra"
assert w["sandbox"] == "read_only"
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

    def test_usage_is_preserved_without_synthetic_zero(self) -> None:
        result = execute(load_request(self._write_request(prompt="UNKNOWN_USAGE")))
        self.assertIsNone(result["usage"])
        self.assertEqual(result["status"], "RESULT_AVAILABLE")

    def test_omitted_profile_defaults_to_astra_high_default(self) -> None:
        from codex_collaboration_harness.embedded_nokiy import _prepare
        payload = json.loads(self.request_path.read_text())
        for key in ("model", "reasoning_effort", "model_acceleration"):
            payload.pop(key)
        self.request_path.write_text(json.dumps(payload))
        request = load_request(self.request_path)
        ready, _, wire = _prepare(request)
        self.assertEqual(wire["provider_profile"], {
            "model": "gpt-6-astra", "reasoning_effort": "high", "service_tier": "default"})
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


if __name__ == "__main__":
    unittest.main()
