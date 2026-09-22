# SPDX-License-Identifier: MIT
"""Contract tests plus opt-in actual Rust/Seatbelt integration tests."""

from __future__ import annotations

import asyncio
import copy
import ctypes
import hashlib
import importlib.util
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codex_collaboration_harness.core import canonical_sha256
from codex_collaboration_harness.graph_entry import (
    GraphEntryError,
    GraphSettings,
    MAX_RESPONSE,
    _artifact_state,
    _failure,
    _native_mcp_settings,
    auto_execute,
    bounded_response,
    build_mcp_server,
    compact_result,
    decode,
    execute,
    invoke_mcp,
    main,
    prepare_graph,
    sandbox_profile,
    verify_context,
)


class GraphFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("NOKIY_GRAPH_TEST_ROOT"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src/value.txt").write_text("before\n")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        engine = Path(os.environ.get("NOKIY_GRAPH_ENGINE", str(self.root / "engine")))
        digest = hashlib.sha256(engine.read_bytes()).hexdigest() if engine.is_file() else "0" * 64
        self.settings = GraphSettings(engine, digest, self.workspace, self.artifacts, "fixture-task")
        self.current = {"fixture-source": "a" * 64}
        self.payload = self.make_payload()

    def rehash(self, payload: dict) -> None:
        contract = payload["request"]["jspace"]
        auth = {"schema_version": "jspace_authorization_v1", **{
            key: contract[key] for key in (
                "repo_root", "matched_surface_ids", "read_scopes", "write_scopes",
                "allowed_operations", "denied_operations", "command_templates",
                "declared_targets", "expansion",
            )
        }, "required_domain_bindings": contract["dcf_generation"]["required_domain_bindings"]}
        contract["authorization_semantic_sha256"] = canonical_sha256(auth)
        contract["content_sha256"] = canonical_sha256({
            k: v for k, v in contract.items() if k != "content_sha256"
        })
        context = payload["context"]
        context["jspace_semantic_sha256"] = contract["authorization_semantic_sha256"]
        context["semantic_sha256"] = canonical_sha256({
            k: v for k, v in context.items() if k != "semantic_sha256"
        })

    def make_payload(self) -> dict:
        contract = {
            "schema_version": "jspace_contract_v2", "repo_root": str(self.workspace),
            "dcf_generation": {
                "repo_root": str(self.workspace), "generation_id": "synthetic-fixture-only",
                "required_domain_bindings": {},
                "action_freshness": {"source_fingerprints": self.current.copy(),
                                     "token_sha256": canonical_sha256(self.current)},
            },
            "provenance": {"kind": "synthetic-fixture"}, "matched_surface_ids": [],
            "read_scopes": ["src/**"], "write_scopes": ["src/**"],
            "allowed_operations": ["read", "create", "modify", "command"],
            "denied_operations": ["network", "install", "delete", "system_mutation"],
            "command_templates": [{"argv": ["/bin/cat", "src/value.txt"],
                                   "effects": ["read"], "targets": []}],
            "focused_verifiers": [], "declared_targets": ["src/value.txt"],
            "expansion": {"mode": "exact_target_only", "error_code": "JSPACE_EXPANSION_REQUIRED",
                          "mutation_on_expansion": False},
        }
        context = {
            "schema_version": "task_context_capsule_v1",
            "mission": {"task_id": "fixture-task", "mission_id": "fixture-mission",
                        "mode": "DELIVERY", "current_predicate": "verified",
                        "objective": "read synthetic fixture only"},
            "dcf_generation": copy.deepcopy(contract["dcf_generation"]),
            "surface": {key: contract[key] for key in
                        ("repo_root", "declared_targets", "matched_surface_ids")},
            "authority": {"authority_effect": "none", "denied_operations": contract["denied_operations"]},
            "context_summary": "Synthetic fixture; never current production evidence.",
            "evidence_refs": [], "focused_verifiers": [],
        }
        payload = {"context": context, "request": {
            "schema_version": "codex_tura_graph_v1", "task_id": "fixture-task", "call_id": "read-1",
            "workspace": str(self.workspace), "jspace": contract,
            "expires_at_unix": int(time.time()) + 120, "timeout_ms": 5000,
            "graph": {"commands": [{"id": "read", "step": 1, "command_type": "shell_command",
                                    "command_line": "/bin/cat src/value.txt"}]}, "preconditions": {},
        }}
        self.rehash(payload)
        return payload

    def verify_fixture(self, contract: dict) -> None:
        # Tests may use synthetic source identities. The installed adapter cannot.
        expected = contract["dcf_generation"]["action_freshness"]
        if (expected["token_sha256"] != canonical_sha256(expected["source_fingerprints"])
                or expected["source_fingerprints"] != self.current):
            raise GraphEntryError("JSPACE_REQUIRED_DOMAIN_CHANGED")


class ContractTests(GraphFixture):
    def test_native_invocation_requires_host_environment_not_request_identity(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(GraphEntryError, "NATIVE_CALLER_ENV_REQUIRED"):
                asyncio.run(invoke_mcp(self.payload, self.settings))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "other-task"}):
            with self.assertRaisesRegex(GraphEntryError, "NATIVE_CALLER_ENV_MISMATCH"):
                asyncio.run(invoke_mcp(self.payload, self.settings))
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_unbound_launcher_does_not_trust_model_supplied_task_identity(self) -> None:
        for caller in (None, "", "../foreign"):
            with self.subTest(caller=caller):
                settings = replace(self.settings, caller_task_id=caller)
                with self.assertRaisesRegex(GraphEntryError, "CALLER_TASK_BINDING_REQUIRED"):
                    verify_context(self.payload, settings, self.verify_fixture)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_exact_context_and_caller_binding(self) -> None:
        request, _ = verify_context(self.payload, self.settings, self.verify_fixture)
        self.assertEqual(request["task_id"], "fixture-task")

    def test_context_digest_drift_is_rejected(self) -> None:
        self.payload["context"]["context_summary"] = "drift"
        with self.assertRaisesRegex(GraphEntryError, "CONTEXT_DIGEST"):
            verify_context(self.payload, self.settings, self.verify_fixture)

    def test_self_consistent_foreign_task_is_rejected(self) -> None:
        self.payload["request"]["task_id"] = "other-task"
        self.payload["context"]["mission"]["task_id"] = "other-task"
        self.rehash(self.payload)
        with self.assertRaisesRegex(GraphEntryError, "CALLER_TASK"):
            verify_context(self.payload, self.settings, self.verify_fixture)

    def test_foreign_context_task_is_rejected(self) -> None:
        self.payload["context"]["mission"]["task_id"] = "other-task"
        self.rehash(self.payload)
        with self.assertRaisesRegex(GraphEntryError, "CONTEXT_TASK"):
            verify_context(self.payload, self.settings, self.verify_fixture)

    def test_live_fingerprint_change_is_not_an_expiry_check(self) -> None:
        self.current["fixture-source"] = "b" * 64
        with self.assertRaisesRegex(GraphEntryError, "REQUIRED_DOMAIN_CHANGED"):
            verify_context(self.payload, self.settings, self.verify_fixture)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_context_generation_change_is_rejected(self) -> None:
        self.payload["context"]["dcf_generation"]["generation_id"] = "other"
        self.rehash(self.payload)
        with self.assertRaisesRegex(GraphEntryError, "GENERATION_MISMATCH"):
            verify_context(self.payload, self.settings, self.verify_fixture)

    def test_missing_context_is_rejected(self) -> None:
        with self.assertRaisesRegex(GraphEntryError, "FIELDS_INVALID"):
            verify_context({"request": self.payload["request"]}, self.settings, self.verify_fixture)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(GraphEntryError, "DUPLICATE_JSON_KEY"):
            decode(b'{"request":{},"request":{}}')

    def test_input_limit_precedes_json_parse(self) -> None:
        with self.assertRaisesRegex(GraphEntryError, "INPUT_LIMIT"):
            decode(b"x" * 524_289)

    def test_budget_is_bounded(self) -> None:
        self.payload["request"]["timeout_ms"] = 120_001
        with self.assertRaisesRegex(GraphEntryError, "BUDGET_INVALID"):
            verify_context(self.payload, self.settings, self.verify_fixture)

    def test_large_result_returns_locator_not_full_transcript(self) -> None:
        result = {"status": "stopped", "task_id": "fixture-task", "graph": {
            "results": [{"id": "x", "success": False, "output": "a" * 100_000}]}}
        compact = compact_result(result, self.artifacts)
        self.assertTrue(compact["output_truncated"])
        self.assertEqual(compact["status"], "stopped")
        self.assertLess(len(json.dumps(compact)), 2000)

    def test_task_artifacts_cannot_escape_via_symlink(self) -> None:
        (self.artifacts / "tura-graph").symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaisesRegex(GraphEntryError, "DIRECTORY_INVALID"):
            _artifact_state(self.settings, "fixture-task")

    def test_task_budget_is_checked_without_pruning_evidence(self) -> None:
        state = _artifact_state(self.settings, "fixture-task")
        (state / "evidence").write_bytes(b"retain")
        with patch("codex_collaboration_harness.graph_entry.MAX_TASK_BYTES", 1):
            with self.assertRaisesRegex(GraphEntryError, "ARTIFACT_BUDGET"):
                _artifact_state(self.settings, "fixture-task")
        self.assertEqual((state / "evidence").read_bytes(), b"retain")

    @unittest.skipUnless(sys.platform == "darwin", "macOS-specific profile")
    def test_unsupported_glob_is_not_silently_broadened(self) -> None:
        self.payload["request"]["jspace"]["write_scopes"] = ["src/*.txt"]
        with self.assertRaisesRegex(GraphEntryError, "SCOPE_UNSUPPORTED"):
            sandbox_profile(self.payload["request"], self.settings, self.artifacts)


class ResponseBoundaryTests(GraphFixture):
    def result_with_bytes(self, size: int) -> dict:
        result = {"status": "completed", "task_id": "fixture-task", "call_id": "read-1",
                  "request_digest": canonical_sha256(self.payload["request"]), "replayed": False,
                  "graph": {"results": [{"id": "read", "success": True,
                                         "output": {"stdout": ""}}]}}
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        result["graph"]["results"][0]["output"]["stdout"] = "x" * (size - len(raw))
        return result

    def assert_bounded(self, result: dict) -> None:
        rendered = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
        self.assertLessEqual(len(rendered), MAX_RESPONSE)

    def test_cli_checks_final_serialization_including_host_metadata(self) -> None:
        raw = self.result_with_bytes(MAX_RESPONSE)
        executor = AsyncMock(return_value=compact_result(raw, self.artifacts))
        args = ["execute", "--engine", str(self.settings.engine), "--engine-sha256", "0" * 64,
                "--dcf-root", str(self.workspace), "--artifact-root", str(self.artifacts)]
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}),
              patch("codex_collaboration_harness.graph_entry.execute", executor),
              patch("codex_collaboration_harness.graph_entry.DcfBoundary"),
              patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(self.payload).encode()))),
              patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(main(args), 0)
        rendered = output.getvalue().encode()
        self.assertLessEqual(len(rendered), MAX_RESPONSE)
        result = json.loads(rendered)
        self.assertTrue(result["output_truncated"])
        self.assertEqual(result["full_result_sha256"], canonical_sha256(raw))
        self.assertEqual(result["full_result_bytes"], MAX_RESPONSE)
        self.assertEqual(result["host_invocation"], "native-shell-task-bound-direct-graph")
        executor.assert_awaited_once()

    def test_late_metadata_and_repeated_projection_keep_original_digest(self) -> None:
        raw = self.result_with_bytes(MAX_RESPONSE - 4500)
        response = compact_result(raw, self.artifacts)
        self.assertNotIn("output_truncated", response)
        response["process_scope"] = {"diagnostic": "x" * 8000}
        compact = bounded_response(response)
        self.assertTrue(compact["output_truncated"])
        self.assertEqual(compact["full_result_sha256"], canonical_sha256(raw))
        self.assertEqual(compact["node_summary"], [{"id": "read", "success": True}])
        self.assertEqual(bounded_response(compact), compact)
        self.assert_bounded(compact)
        replay = compact_result({**raw, "replayed": True}, self.artifacts)
        replay["process_scope"] = response["process_scope"]
        replay = bounded_response(replay)
        self.assertEqual(replay["full_result_sha256"], compact["full_result_sha256"])
        self.assertEqual(replay["full_result_bytes"], compact["full_result_bytes"])
        self.assertTrue(replay["replayed"])

    def test_unicode_and_escaping_are_measured_as_serialized_utf8_bytes(self) -> None:
        raw = self.result_with_bytes(1000)
        raw["graph"]["results"][0]["output"]["stdout"] = '\u4e2d\\"\n' * 20000
        compact = compact_result(raw, self.artifacts)
        self.assertTrue(compact["output_truncated"])
        self.assertEqual(compact["full_result_sha256"], canonical_sha256(raw))
        self.assert_bounded(compact)

    def test_large_diagnostics_block_intake_without_discarding_engine_identity(self) -> None:
        raw = self.result_with_bytes(1000)
        response = compact_result(raw, self.artifacts)
        response["process_scope"] = {"diagnostic": "x" * MAX_RESPONSE}
        limited = bounded_response(response)
        self.assertEqual(limited["status"], "blocked")
        self.assertEqual(limited["error"], "GRAPH_RESPONSE_BUDGET_EXCEEDED")
        self.assertEqual(limited["observed_status"], "completed")
        self.assertEqual(limited["full_result_sha256"], canonical_sha256(raw))
        self.assertFalse(limited["retry_safe"])
        self.assertFalse(limited["fallback_performed"])
        self.assertEqual(bounded_response(limited), limited)
        self.assert_bounded(limited)

    def test_large_exception_has_no_invented_archive_or_engine_provenance(self) -> None:
        result = _failure(GraphEntryError("\u4e2d" * MAX_RESPONSE))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["effect_state"], "unproven")
        self.assertFalse(result["retry_safe"])
        self.assertNotIn("full_result_sha256", result)
        self.assertNotIn("recovery_artifact", result)
        self.assert_bounded(result)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "requires graph optional SDK")
    def test_actual_mcp_sdk_text_and_structured_projection_are_bounded(self) -> None:
        unicode_result = self.result_with_bytes(1000)
        unicode_result["graph"]["results"][0]["output"]["stdout"] = "\u4e2d" * 20000
        for raw in (self.result_with_bytes(MAX_RESPONSE), unicode_result):
            with self.subTest(unicode=raw is unicode_result):
                server = build_mcp_server(self.settings, SimpleNamespace(verify=self.verify_fixture))
                executor = AsyncMock(return_value=compact_result(raw, self.artifacts))
                with patch("codex_collaboration_harness.graph_entry.execute", executor):
                    content, structured = asyncio.run(server.call_tool("nokiy_graph_execute", self.payload))
                text = [item.text for item in content if item.type == "text"]
                self.assertEqual(len(text), 1)
                self.assertLessEqual(len(text[0].encode()), MAX_RESPONSE)
                self.assertEqual(json.loads(text[0]), structured)
                self.assertEqual(structured["full_result_sha256"], canonical_sha256(raw))
                self.assert_bounded(structured)
                executor.assert_awaited_once()

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "requires graph optional SDK")
    def test_mcp_float_serialization_is_measured_at_the_exact_boundary(self) -> None:
        raw = self.result_with_bytes(1000)
        response = compact_result(raw, self.artifacts)
        response["process_scope"] = {"elapsed": 1e-5, "padding": ""}
        size = len((json.dumps(response, ensure_ascii=False, indent=2) + "\n").encode())
        response["process_scope"]["padding"] = "x" * (MAX_RESPONSE - size)
        from pydantic_core import to_json

        self.assertGreater(len(to_json(response, indent=2)), MAX_RESPONSE)
        server = build_mcp_server(self.settings, SimpleNamespace(verify=self.verify_fixture))
        with patch("codex_collaboration_harness.graph_entry.execute", AsyncMock(return_value=response)):
            content, structured = asyncio.run(server.call_tool("nokiy_graph_execute", self.payload))
        self.assertLessEqual(len(content[0].text.encode()), MAX_RESPONSE)
        self.assertTrue(structured["output_truncated"])
        self.assertEqual(structured["full_result_sha256"], canonical_sha256(raw))
        self.assertEqual(json.loads(content[0].text), structured)


class NativeMcpBindingTests(GraphFixture):
    def test_native_sandbox_denied_before_artifact_or_subprocess(self) -> None:
        with patch("sys.platform", "darwin"), patch.dict(os.environ, {"CODEX_SANDBOX": "seatbelt"}), \
                patch("codex_collaboration_harness.graph_entry._artifact_state") as state, \
                patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as launch:
            with self.assertRaisesRegex(GraphEntryError, "HOST_SEATBELT_REENTRY_UNSUPPORTED"):
                asyncio.run(execute(self.payload, self.settings, self.verify_fixture))
            state.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_metadata_never_uses_an_inherited_parent(self) -> None:
        settings = replace(self.settings, caller_task_id=None)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign-parent"}):
            for metadata in (None, {}, {"task_id": "fixture-task"}, {"threadId": "../escape"},
                             {"threadId": ""}, {"threadId": 17}):
                with self.subTest(metadata=metadata), self.assertRaisesRegex(GraphEntryError, "TASK_METADATA_REQUIRED"):
                    _native_mcp_settings(settings, metadata)
        with self.assertRaisesRegex(GraphEntryError, "STATIC_CALLER_FORBIDDEN"):
            _native_mcp_settings(self.settings, {"threadId": "fixture-task"})

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "requires graph optional SDK")
    def test_task_binding_is_request_local_and_context_is_still_verified(self) -> None:
        from mcp.types import RequestParams

        settings = replace(self.settings, caller_task_id=None)
        first = _native_mcp_settings(settings, RequestParams.Meta(threadId="fixture-task"))
        second = _native_mcp_settings(settings, {"threadId": "other-task"})
        self.assertIsNone(settings.caller_task_id)
        self.assertEqual(first.caller_task_id, "fixture-task")
        self.assertEqual(second.caller_task_id, "other-task")
        verify_context(self.payload, first, self.verify_fixture)
        with self.assertRaisesRegex(GraphEntryError, "CALLER_TASK_MISMATCH"):
            verify_context(self.payload, second, self.verify_fixture)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "requires graph optional SDK")
    def test_registered_schema_excludes_sdk_context_and_metadata(self) -> None:
        server = build_mcp_server(replace(self.settings, caller_task_id=None),
                                  SimpleNamespace(verify=self.verify_fixture), native_metadata=True)
        tools = asyncio.run(server.list_tools())
        self.assertEqual([tool.name for tool in tools], ["nokiy_graph_execute", "nokiy_graph_prepare"])
        self.assertEqual(set(tools[0].inputSchema["properties"]), {"request", "context"})
        self.assertEqual(set(tools[1].inputSchema["properties"]), {"specification"})
        self.assertFalse(tools[0].annotations.readOnlyHint)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "requires graph optional SDK")
    def test_native_handler_binds_sdk_metadata_without_extra_execution(self) -> None:
        server = build_mcp_server(replace(self.settings, caller_task_id=None),
                                  SimpleNamespace(verify=self.verify_fixture), native_metadata=True)
        handler = server._tool_manager.get_tool("nokiy_graph_execute").fn
        ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": "fixture-task"}))
        executor = AsyncMock(return_value={"status": "completed"})
        with patch("codex_collaboration_harness.graph_entry.execute", executor):
            result = asyncio.run(handler(self.payload["request"], self.payload["context"], ctx))
        executor.assert_awaited_once()
        self.assertEqual(executor.await_args.args[1].caller_task_id, "fixture-task")
        self.assertEqual(result["caller_task_binding"], "mcp-request-metadata-threadId")
        ctx.request_context.meta = None
        with patch("codex_collaboration_harness.graph_entry.execute", new_callable=AsyncMock) as blocked:
            result = asyncio.run(handler(self.payload["request"], self.payload["context"], ctx))
            blocked.assert_not_called()
        self.assertEqual(result["error"], "GRAPH_NATIVE_MCP_TASK_METADATA_REQUIRED")
        self.assertFalse(result["retry_safe"])
        self.assertFalse(result["fallback_performed"])

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "requires graph optional SDK")
    def test_compatibility_server_keeps_exact_single_tool_inventory(self) -> None:
        server = build_mcp_server(self.settings, SimpleNamespace(verify=self.verify_fixture))
        self.assertEqual([tool.name for tool in asyncio.run(server.list_tools())], ["nokiy_graph_execute"])


class GraphPreparationTests(GraphFixture):
    def setUp(self) -> None:
        super().setUp()
        self.specification = {
            "call_id": "prepared-fixture", "surface_id": "fixture_surface",
            "mission": {k: v for k, v in self.payload["context"]["mission"].items() if k != "task_id"},
            "action": {"operations": ["read", "command"], "read_scopes": ["src/**"], "write_scopes": []},
            "graph": self.payload["request"]["graph"], "timeout_ms": 5000,
        }
        self.boundary = SimpleNamespace(
            compile=Mock(return_value=(copy.deepcopy(self.payload["request"]["jspace"]),
                                       copy.deepcopy(self.payload["context"]))),
            verify=self.verify_fixture,
        )

    def test_reuses_dcf_compiler_with_host_identity_and_creates_no_execution_state(self) -> None:
        before = copy.deepcopy(self.specification)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as launch:
            prepared = prepare_graph(self.specification, self.settings, self.boundary)
        launch.assert_not_called()
        self.assertEqual(self.specification, before)
        self.assertEqual(list(self.artifacts.iterdir()), [])
        self.boundary.compile.assert_called_once()
        self.assertEqual(self.boundary.compile.call_args.args[1]["mission"]["task_id"], "fixture-task")
        self.assertEqual(prepared["request"]["call_id"], "prepared-fixture")
        verify_context(prepared, self.settings, self.verify_fixture)

    def test_model_cannot_supply_task_or_nested_mission(self) -> None:
        for key in ("task_id", "mission", "action"):
            spec = copy.deepcopy(self.specification)
            if key == "task_id":
                spec[key] = "foreign"
            elif key == "mission":
                spec[key]["task_id"] = "foreign"
            else:
                spec[key]["mission"] = {"task_id": "foreign"}
            with self.subTest(key=key), self.assertRaises(GraphEntryError):
                prepare_graph(spec, self.settings, self.boundary)
        self.boundary.compile.assert_not_called()

    def test_invalid_identity_budget_or_missing_field_never_compiles(self) -> None:
        for change in ({"call_id": "../escape"}, {"surface_id": ""}, {"timeout_ms": True},
                       {"timeout_ms": 120001}):
            with self.subTest(change=change), self.assertRaises(GraphEntryError):
                prepare_graph({**self.specification, **change}, self.settings, self.boundary)
        spec = {k: v for k, v in self.specification.items() if k != "mission"}
        with self.assertRaises(GraphEntryError):
            prepare_graph(spec, self.settings, self.boundary)
        self.boundary.compile.assert_not_called()

    def test_compiler_or_freshness_failure_never_executes_or_falls_back(self) -> None:
        self.boundary.compile.side_effect = GraphEntryError("JSPACE_REQUIRED_DOMAIN_CHANGED")
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as launch:
            with self.assertRaisesRegex(GraphEntryError, "JSPACE_REQUIRED_DOMAIN_CHANGED"):
                prepare_graph(self.specification, self.settings, self.boundary)
        launch.assert_not_called()
        self.assertEqual(list(self.artifacts.iterdir()), [])


class PreparationCliTests(GraphPreparationTests):
    def arguments(self) -> list[str]:
        return ["prepare", "--engine", str(self.settings.engine),
                "--engine-sha256", self.settings.engine_sha256,
                "--dcf-root", str(self.workspace), "--artifact-root", str(self.artifacts)]

    def call_prepare(self, spec: dict, extra: list[str] | None = None) -> tuple[int, dict]:
        with (patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(spec).encode()))),
              patch("sys.stdout", new_callable=io.StringIO) as output):
            code = main(self.arguments() + (extra or []))
        return code, json.loads(output.getvalue())

    def test_direct_preparation_uses_existing_compiler_and_returns_exact_envelope(self) -> None:
        self.specification["preconditions"] = {"read": {"src/value.txt": "b" * 64}}
        before = copy.deepcopy(self.specification)
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}),
              patch("codex_collaboration_harness.graph_entry.DcfBoundary", return_value=self.boundary),
              patch("codex_collaboration_harness.graph_entry.execute") as executor,
              patch("codex_collaboration_harness.graph_entry.invoke_mcp") as mcp,
              patch("asyncio.create_subprocess_exec") as launch,
              patch("codex_collaboration_harness.graph_entry.time.time", return_value=1000)):
            code, prepared = self.call_prepare(self.specification)
            self.assertEqual(code, 0)
            self.assertEqual(set(prepared), {"request", "context"})
            verify_context(prepared, self.settings, self.verify_fixture)
        self.assertEqual(prepared["request"]["expires_at_unix"], 1180)
        self.assertEqual(prepared["request"]["task_id"], "fixture-task")
        for key in ("call_id", "preconditions", "graph"):
            self.assertEqual(prepared["request"][key], self.specification[key])
        self.assertEqual(self.specification, before)
        self.assertEqual(list(self.artifacts.iterdir()), [])
        self.boundary.compile.assert_called_once()
        executor.assert_not_called()
        mcp.assert_not_called()
        launch.assert_not_called()

    def test_cli_identity_rejection_precedes_dcf_or_stdin(self) -> None:
        for environment, extra in (({}, []), ({"CODEX_THREAD_ID": "fixture-task"},
                                             ["--caller-task-id", "foreign"])):
            with (patch.dict(os.environ, environment, clear=True),
                  patch("codex_collaboration_harness.graph_entry.DcfBoundary") as boundary,
                  patch("sys.stdin", object()),
                  patch("sys.stdout", new_callable=io.StringIO) as output):
                self.assertEqual(main(self.arguments() + extra), 2)
            self.assertIn("GRAPH_NATIVE_CALLER_ENV_", json.loads(output.getvalue())["error"])
            boundary.assert_not_called()

    def test_cli_invalid_input_and_stale_context_fail_without_dispatch(self) -> None:
        for spec, stale, expected in (
            ({**self.specification, "task_id": "foreign"}, False, "SPECIFICATION_INVALID"),
            ({**self.specification, "call_id": "../escape"}, False, "IDENTITY_INVALID"),
            ({**self.specification, "timeout_ms": 120001}, False, "BUDGET_INVALID"),
            (self.specification, True, "JSPACE_REQUIRED_DOMAIN_CHANGED"),
        ):
            self.boundary.compile.reset_mock()
            self.boundary.compile.side_effect = (GraphEntryError(expected) if stale else None)
            with (self.subTest(expected=expected),
                  patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}),
                  patch("codex_collaboration_harness.graph_entry.DcfBoundary", return_value=self.boundary),
                  patch("asyncio.create_subprocess_exec") as launch):
                code, result = self.call_prepare(spec)
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "blocked")
            self.assertIn(expected, result["error"])
            self.assertFalse(result["fallback_performed"])
            launch.assert_not_called()
            if not stale:
                self.boundary.compile.assert_not_called()
        self.assertEqual(list(self.artifacts.iterdir()), [])


class InstalledSettingsTests(GraphFixture):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.root / "graph.json"
        self.values = {
            "engine": str(self.settings.engine),
            "engine_sha256": self.settings.engine_sha256,
            "dcf_root": str(self.workspace),
            "artifact_root": str(self.artifacts),
        }
        self.config.write_text(json.dumps(self.values))

    def call_cli(self, argv: list[str]) -> tuple[int, dict, AsyncMock]:
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(self.payload).encode()))
        output = io.StringIO()
        invoke = AsyncMock(return_value={"status": "completed"})
        with stdin, patch("sys.stdin", stdin), patch("sys.stdout", output), \
                patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}), \
                patch("codex_collaboration_harness.graph_entry.invoke_mcp", invoke), \
                patch("codex_collaboration_harness.graph_entry.DcfBoundary") as boundary:
            code = main(argv)
            boundary.assert_not_called()
        return code, json.loads(output.getvalue()), invoke

    def test_default_location_does_not_persist_caller_identity(self) -> None:
        destination = self.root / ".config/codex-collaboration-harness/graph.json"
        destination.parent.mkdir(parents=True)
        self.config.rename(destination)
        with patch("codex_collaboration_harness.graph_entry.Path.home", return_value=self.root):
            code, _, invoke = self.call_cli(["invoke"])
        self.assertEqual(code, 0)
        self.assertEqual(invoke.await_args.args[1], self.settings)
        self.assertNotIn("caller_task_id", json.loads(destination.read_text()))

    def test_explicit_option_overrides_only_its_config_field(self) -> None:
        alternate = self.root / "alternate-engine"
        code, _, invoke = self.call_cli([
            "invoke", "--config", str(self.config), "--engine", str(alternate),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(invoke.await_args.args[1], replace(self.settings, engine=alternate))

    def test_complete_explicit_settings_do_not_read_missing_config(self) -> None:
        argv = ["invoke", "--config", str(self.root / "missing.json")]
        for key, value in self.values.items():
            argv.extend(["--" + key.replace("_", "-"), value])
        code, _, invoke = self.call_cli(argv)
        self.assertEqual(code, 0)
        self.assertEqual(invoke.await_args.args[1], self.settings)

    def test_invalid_settings_never_dispatch(self) -> None:
        invalid = [
            {**self.values, "caller_task_id": "foreign-task"},
            {key: value for key, value in self.values.items() if key != "engine"},
            {**self.values, "engine": "   "},
            {**self.values, "engine": 7},
            {**self.values, "artifact_root": None},
        ]
        for values in invalid:
            with self.subTest(values=values):
                self.config.write_text(json.dumps(values))
                code, result, invoke = self.call_cli(["invoke", "--config", str(self.config)])
                self.assertEqual(code, 2)
                self.assertEqual(result["error"], "GRAPH_INSTALL_SETTINGS_INVALID")
                invoke.assert_not_awaited()

    def test_duplicate_config_key_is_rejected(self) -> None:
        self.config.write_text('{"engine":"a","engine":"b"}')
        code, result, invoke = self.call_cli(["invoke", "--config", str(self.config)])
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "GRAPH_DUPLICATE_JSON_KEY")
        invoke.assert_not_awaited()

    def test_missing_or_symlink_settings_never_dispatch(self) -> None:
        self.config.unlink()
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                if symlink:
                    self.config.symlink_to(self.workspace / "src/value.txt")
                code, result, invoke = self.call_cli(["invoke", "--config", str(self.config)])
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "blocked")
                invoke.assert_not_awaited()
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_direct_entry_uses_same_executor_without_mcp(self) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(self.payload).encode()))
        output = io.StringIO()
        executor = AsyncMock(return_value={"status": "completed"})
        with stdin, patch("sys.stdin", stdin), patch("sys.stdout", output), \
                patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}), \
                patch("codex_collaboration_harness.graph_entry.execute", executor), \
                patch("codex_collaboration_harness.graph_entry.invoke_mcp") as mcp, \
                patch("codex_collaboration_harness.graph_entry.DcfBoundary") as boundary:
            code = main(["execute", "--config", str(self.config)])
            self.assertEqual(code, 0)
            executor.assert_awaited_once_with(self.payload, self.settings, boundary.return_value.verify)
            mcp.assert_not_called()
            boundary.assert_called_once_with(self.workspace)
        result = json.loads(output.getvalue())
        self.assertEqual(result["host_invocation"], "native-shell-task-bound-direct-graph")
        self.assertIs(result["mcp_transport_used"], False)
        self.assertIs(result["direct_native_mcp_registration"], False)

    def test_shell_entries_reject_missing_or_overridden_native_identity_before_dcf(self) -> None:
        for mode in ("auto", "execute", "invoke"):
            for task, override, expected in (
                    (None, "fixture-task", "GRAPH_NATIVE_CALLER_ENV_REQUIRED"),
                    ("fixture-task", "foreign-task", "GRAPH_NATIVE_CALLER_ENV_MISMATCH"),
                    ("../invalid", None, "GRAPH_NATIVE_CALLER_ENV_REQUIRED")):
                with self.subTest(mode=mode, task=task, override=override):
                    output = io.StringIO()
                    env = {"CODEX_THREAD_ID": task} if task is not None else {}
                    argv = [mode, "--config", str(self.config)]
                    if override:
                        argv.extend(["--caller-task-id", override])
                    with patch.dict(os.environ, env, clear=True), patch("sys.stdout", output), \
                            patch("codex_collaboration_harness.graph_entry.execute") as executor, \
                            patch("codex_collaboration_harness.graph_entry.invoke_mcp") as mcp, \
                            patch("codex_collaboration_harness.graph_entry.DcfBoundary") as boundary:
                        self.assertEqual(main(argv), 2)
                        boundary.assert_not_called()
                        executor.assert_not_called()
                        mcp.assert_not_called()
                    self.assertEqual(json.loads(output.getvalue())["error"], expected)
        self.assertEqual(list(self.artifacts.iterdir()), [])


class AutoRoutingTests(GraphFixture):
    def setUp(self) -> None:
        super().setUp()
        self.payload["request"]["jspace"]["write_scopes"] = []
        self.payload["request"]["jspace"]["allowed_operations"] = ["read", "command"]
        self.rehash(self.payload)

    def run_auto(self) -> dict:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}):
            return asyncio.run(auto_execute(self.payload, self.settings, self.verify_fixture))

    def test_independent_reads_recommend_native_without_executing_or_creating_state(self) -> None:
        for count in (1, 2, 8):
            with self.subTest(count=count):
                node = self.payload["request"]["graph"]["commands"][0]
                self.payload["request"]["graph"]["commands"] = [
                    {**node, "id": f"read-{index}"} for index in range(count)]
                before = copy.deepcopy(self.payload)
                with patch("codex_collaboration_harness.graph_entry.execute") as executor, \
                        patch("codex_collaboration_harness.graph_entry.invoke_mcp") as mcp:
                    first = self.run_auto()
                    self.assertEqual(first, self.run_auto())
                    executor.assert_not_called()
                    mcp.assert_not_called()
                self.assertEqual(first["status"], "native_required")
                self.assertFalse(first["execution_started"])
                self.assertFalse(first["call_identity_reserved"])
                self.assertFalse(first["route_selection"]["authorization_granted"])
                self.assertEqual(
                    first["route_selection"]["reason"],
                    "independent_reads_prefer_native_batching",
                )
                self.assertEqual(first["prior_effect_state"], "not_inferred")
                self.assertEqual(self.payload, before)
                self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_ordered_read_only_commands_recommend_one_native_batch(self) -> None:
        node = self.payload["request"]["graph"]["commands"][0]
        self.payload["request"]["graph"]["commands"] = [
            {**node, "id": f"read-{step}", "step": step}
            for step in range(1, 5)
        ]
        self.rehash(self.payload)
        before = copy.deepcopy(self.payload)
        with patch("codex_collaboration_harness.graph_entry.execute") as executor:
            result = self.run_auto()
        executor.assert_not_called()
        self.assertEqual(result["status"], "native_required")
        self.assertEqual(result["route_selection"]["selected_route"], "native")
        self.assertEqual(
            result["route_selection"]["reason"],
            "ordered_read_only_commands_prefer_native_batching",
        )
        self.assertFalse(result["execution_started"])
        self.assertEqual(self.payload, before)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_graph_requirements_reuse_exact_executor_and_input(self) -> None:
        for kind in ("binding", "write", "precondition", "unknown_control", "sparse_order"):
            with self.subTest(kind=kind):
                payload = copy.deepcopy(self.payload)
                request = payload["request"]
                node = request["graph"]["commands"][0]
                if kind == "binding":
                    node["command_line"] = "/bin/cat '#@#${prior.stdout}#@#$'"
                elif kind == "write":
                    request["jspace"]["write_scopes"] = ["src/**"]
                elif kind == "precondition":
                    request["preconditions"] = {"src/value.txt": "a" * 64}
                elif kind == "unknown_control":
                    request["graph"]["future-control"] = True
                else:
                    node["step"] = 2
                self.rehash(payload)
                executor = AsyncMock(return_value={"status": "completed"})
                with patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}), \
                        patch("codex_collaboration_harness.graph_entry.execute", executor):
                    result = asyncio.run(auto_execute(payload, self.settings, self.verify_fixture))
                executor.assert_awaited_once_with(payload, self.settings, self.verify_fixture)
                self.assertEqual(result["route_selection"]["selected_route"], "graph")

    def test_unknown_request_fields_fail_before_recommendation_or_dispatch(self) -> None:
        for field in ("future_control", "caller_task_id", "precondition"):
            for ordered in (False, True):
                with self.subTest(field=field, ordered=ordered):
                    payload = copy.deepcopy(self.payload)
                    payload["request"][field] = True
                    if ordered:
                        payload["request"]["graph"]["commands"][0]["step"] = 2
                    with patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}), \
                            patch("codex_collaboration_harness.graph_entry.execute") as executor, \
                            patch("codex_collaboration_harness.graph_entry.invoke_mcp") as mcp:
                        with self.assertRaisesRegex(GraphEntryError, "GRAPH_REQUEST_FIELDS_INVALID"):
                            asyncio.run(auto_execute(payload, self.settings, self.verify_fixture))
                        executor.assert_not_called()
                        mcp.assert_not_called()
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_missing_required_request_fields_fail_before_freshness_or_dispatch(self) -> None:
        for field in set(self.payload["request"]) - {"preconditions"}:
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload["request"].pop(field)
                with patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}), \
                        patch("codex_collaboration_harness.graph_entry.execute") as executor, \
                        patch.object(self, "verify_fixture") as freshness:
                    with self.assertRaisesRegex(GraphEntryError, "GRAPH_REQUEST_FIELDS_INVALID"):
                        asyncio.run(auto_execute(payload, self.settings, freshness))
                    freshness.assert_not_called()
                    executor.assert_not_called()
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_optional_preconditions_preserve_rust_default_without_rewriting_request(self) -> None:
        self.payload["request"].pop("preconditions")
        before = copy.deepcopy(self.payload)
        with patch("codex_collaboration_harness.graph_entry.execute") as executor:
            result = self.run_auto()
            executor.assert_not_called()
        self.assertEqual(result["status"], "native_required")
        self.assertEqual(result["request_digest"], canonical_sha256(before["request"]))
        self.assertEqual(self.payload, before)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_graph_failure_never_falls_back_to_native_or_mcp(self) -> None:
        self.payload["request"]["graph"]["commands"][0]["step"] = 2
        for failure in (GraphEntryError("EXACT_GRAPH_DENIAL"),
                        {"status": "blocked", "effect_state": "unproven", "retry_safe": False}):
            with self.subTest(failure=failure):
                executor = (AsyncMock(side_effect=failure) if isinstance(failure, Exception)
                            else AsyncMock(return_value=failure))
                with patch("codex_collaboration_harness.graph_entry.execute", executor), \
                        patch("codex_collaboration_harness.graph_entry.invoke_mcp") as mcp:
                    if isinstance(failure, Exception):
                        with self.assertRaisesRegex(GraphEntryError, "EXACT_GRAPH_DENIAL"):
                            self.run_auto()
                    else:
                        result = self.run_auto()
                        self.assertEqual(result["status"], "blocked")
                        self.assertFalse(result["route_selection"]["fallback_performed"])
                    executor.assert_awaited_once()
                    mcp.assert_not_called()

    def test_existing_call_or_partial_closeout_cannot_downgrade_to_native(self) -> None:
        state = _artifact_state(self.settings, "fixture-task")
        key = canonical_sha256(["fixture-task", "read-1"])
        for suffix in (".json", ".tmp", ".json.gz", ".json.gz.tmp", ".reconciliation.tmp"):
            with self.subTest(suffix=suffix):
                artifact = state / (key + suffix)
                artifact.write_bytes(b"unsettled evidence")
                executor = AsyncMock(return_value={"status": "blocked", "retry_safe": False})
                with patch("codex_collaboration_harness.graph_entry.execute", executor):
                    result = self.run_auto()
                self.assertEqual(result["route_selection"]["reason"],
                                 "existing_call_requires_graph_recovery")
                executor.assert_awaited_once()
                self.assertEqual(artifact.read_bytes(), b"unsettled evidence")
                artifact.unlink()

    def test_invalid_or_stale_context_never_becomes_native_recommendation(self) -> None:
        for kind in ("missing", "digest", "stale", "foreign", "scope"):
            with self.subTest(kind=kind):
                payload = copy.deepcopy(self.payload)
                if kind == "missing":
                    payload.pop("context")
                elif kind == "digest":
                    payload["context"]["context_summary"] = "changed"
                elif kind == "stale":
                    payload["request"]["expires_at_unix"] = 0
                elif kind == "foreign":
                    payload["request"]["task_id"] = "foreign"
                else:
                    payload["context"]["surface"]["declared_targets"] = ["foreign.txt"]
                    self.rehash(payload)
                with patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}), \
                        patch("codex_collaboration_harness.graph_entry.execute") as executor:
                    with self.assertRaises(GraphEntryError):
                        asyncio.run(auto_execute(payload, self.settings, self.verify_fixture))
                    executor.assert_not_called()
        self.current["fixture-source"] = "changed"
        with self.assertRaisesRegex(GraphEntryError, "REQUIRED_DOMAIN_CHANGED"):
            self.run_auto()
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_invalid_graph_identity_and_node_budget_fail_closed(self) -> None:
        for field, value in (("call_id", "../escape"), ("schema_version", "unknown"),
                             ("graph", {"commands": []}),
                             ("graph", {"commands": [{}] * 21})):
            with self.subTest(field=field, value=value):
                payload = copy.deepcopy(self.payload)
                payload["request"][field] = value
                with patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}):
                    with self.assertRaises(GraphEntryError):
                        asyncio.run(auto_execute(payload, self.settings, self.verify_fixture))
        node = self.payload["request"]["graph"]["commands"][0]
        self.payload["request"]["graph"]["commands"].append(copy.deepcopy(node))
        with self.assertRaisesRegex(GraphEntryError, "NODE_IDENTITY_INVALID"):
            self.run_auto()

    def test_symlinked_history_is_not_treated_as_absent(self) -> None:
        (self.artifacts / "tura-graph").symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaisesRegex(GraphEntryError, "DIRECTORY_INVALID"):
            self.run_auto()

    def test_native_recommendation_has_distinct_cli_status(self) -> None:
        config = self.root / "graph.json"
        config.write_text(json.dumps({key: str(getattr(self.settings, key)) for key in
                                     ("engine", "engine_sha256", "dcf_root", "artifact_root")}))
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(self.payload).encode()))
        output = io.StringIO()
        with stdin, patch("sys.stdin", stdin), patch("sys.stdout", output), \
                patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}), \
                patch("codex_collaboration_harness.graph_entry.DcfBoundary") as boundary:
            boundary.return_value.verify.side_effect = self.verify_fixture
            self.assertEqual(main(["auto", "--config", str(config)]), 3)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "native_required")
        self.assertEqual(result["host_invocation"], "native-shell-task-bound-auto-selection")
        self.assertFalse(result["mcp_transport_used"])
        self.assertEqual(list(self.artifacts.iterdir()), [])


class SupervisorDiagnosticsTests(GraphFixture):
    def test_invalid_protocol_retains_identity_and_bounded_observation_without_retry(self) -> None:
        engine = self.root / "synthetic-engine"
        engine.write_bytes(b"not executable; subprocess is mocked")
        settings = replace(self.settings, engine=engine,
                           engine_sha256=hashlib.sha256(engine.read_bytes()).hexdigest())
        stderr = b"private diagnostic sentinel; must not enter model output"

        async def observe(stdout: bytes) -> tuple[dict, AsyncMock]:
            out, err = asyncio.StreamReader(), asyncio.StreamReader()
            out.feed_data(stdout)
            out.feed_eof()
            err.feed_data(stderr)
            err.feed_eof()
            process = SimpleNamespace(
                pid=424242, returncode=1, stdin=Mock(drain=AsyncMock()),
                stdout=out, stderr=err, wait=AsyncMock(return_value=1),
            )
            launch = AsyncMock(return_value=process)
            with patch("codex_collaboration_harness.graph_entry.asyncio.create_subprocess_exec", launch), \
                    patch("codex_collaboration_harness.graph_entry.sandbox_profile", return_value="mock-only"):
                result = await execute(self.payload, settings, self.verify_fixture)
            process.wait.assert_awaited_once()
            return result, launch

        for stdout in (b"", b"private stdout sentinel", b"[]", b'{"a":1,"a":2}', b"\xff"):
            with self.subTest(stdout=stdout):
                result, launch = asyncio.run(observe(stdout))
                self.assertEqual(result["error"], "GRAPH_SUPERVISOR_RESULT_INVALID_EFFECT_UNSETTLED")
                self.assertEqual(result["status"], "blocked")
                self.assertIs(result["retry_safe"], False)
                self.assertIs(result["fallback_performed"], False)
                self.assertEqual(result["effect_state"], "unproven")
                self.assertEqual(result["task_id"], "fixture-task")
                self.assertEqual(result["call_id"], "read-1")
                self.assertEqual(result["request_digest"], canonical_sha256(self.payload["request"]))
                observation = result["supervisor_observation"]
                self.assertEqual(observation, {
                    "pid": 424242, "exit_code": 1,
                    "stdout_bytes": len(stdout), "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                    "stderr_bytes": len(stderr), "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                })
                self.assertNotIn("private", json.dumps(result))
                self.assertNotIn("process_scope", result)
                launch.assert_awaited_once()
        self.assertEqual((self.workspace / "src/value.txt").read_text(), "before\n")
        self.assertFalse(any(path.is_file() for path in self.artifacts.rglob("*")))


@unittest.skipUnless(os.environ.get("NOKIY_GRAPH_ENGINE") and sys.platform == "darwin",
                     "requires explicitly selected actual Rust engine on macOS")
class ActualEngineTests(GraphFixture):
    def run_graph(self) -> dict:
        return asyncio.run(execute(self.payload, self.settings, self.verify_fixture))

    def script(self, text: str) -> None:
        (self.workspace / "src/check.py").write_text("print('CANARY_STARTED', flush=True)\n" + text)
        argv = [sys.executable, "-B", "src/check.py"]
        self.payload["request"]["graph"]["commands"][0]["command_line"] = shlex.join(argv)
        self.payload["request"]["jspace"]["command_templates"] = [
            {"argv": argv, "effects": ["read"], "targets": []}]
        self.rehash(self.payload)

    def test_actual_sandboxed_engine_and_cached_replay(self) -> None:
        first = self.run_graph()
        self.assertEqual(first["status"], "completed", first)
        self.assertIn("before", json.dumps(first))
        self.assertEqual(self.run_graph()["replayed"], True)
        self.assertFalse((self.workspace / ".tura").exists())

    def test_automatic_selection_preserves_actual_graph_replay(self) -> None:
        self.payload["request"]["jspace"]["write_scopes"] = []
        self.payload["request"]["jspace"]["allowed_operations"] = ["read", "command"]
        self.rehash(self.payload)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-task"}):
            native = asyncio.run(auto_execute(self.payload, self.settings, self.verify_fixture))
            self.assertEqual(native["status"], "native_required")
            self.assertEqual(list(self.artifacts.iterdir()), [])
            first = self.run_graph()
            self.assertEqual(first["status"], "completed", first)
            replay = asyncio.run(auto_execute(self.payload, self.settings, self.verify_fixture))
        self.assertEqual(replay["status"], "completed", replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["request_digest"], first["request_digest"])
        self.assertEqual(replay["route_selection"]["reason"],
                         "existing_call_requires_graph_recovery")

    def test_installed_retention_verifier_counts_and_checks_rollback_guard(self) -> None:
        # Load only the source verifier; keep product imports isolated to the installed package.
        verifier_path = Path(__file__).resolve().parents[1] / "scripts/graph_readonly_smoke.py"
        spec = importlib.util.spec_from_file_location("graph_retention_smoke_verifier", verifier_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        verify_terminal_retention = verifier.verify_terminal_retention

        first = self.run_graph()
        self.assertEqual(first["status"], "completed", first)
        retained = verify_terminal_retention(first, self.artifacts, "fixture-task")
        self.assertEqual(retained["retained_files"], 2)
        self.assertGreater(retained["net_reclaimed_logical_bytes"], 0)
        self.assertEqual(self.run_graph()["replayed"], True)
        self.assertEqual(verify_terminal_retention(first, self.artifacts, "fixture-task"), retained)
        guard = Path(retained["rollback_guard_path"])
        value = json.loads(guard.read_bytes())
        value["request_digest"] = "0" * 64
        guard.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "GRAPH_ROLLBACK_GUARD_MISMATCH"):
            verify_terminal_retention(first, self.artifacts, "fixture-task")

    def test_actual_network_denial(self) -> None:
        self.script("import socket\nsocket.socket().connect(('127.0.0.1', 9))\n")
        result = self.run_graph()
        self.assertNotEqual(result["status"], "completed", result)
        self.assertIn("Operation not permitted", json.dumps(result))
        self.assertIn("CANARY_STARTED", result["graph"]["results"][0]["output"]["stdout"])

    def test_declared_read_command_cannot_write_outside_scope(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("retain")
        self.script(f"from pathlib import Path\nPath({str(outside)!r}).write_text('bad')\n")
        result = self.run_graph()
        self.assertNotEqual(result["status"], "completed", result)
        self.assertEqual(outside.read_text(), "retain")
        self.assertIn("CANARY_STARTED", result["graph"]["results"][0]["output"]["stdout"])

    def test_declared_read_command_cannot_delete_scoped_source(self) -> None:
        self.script("from pathlib import Path\nPath('src/value.txt').unlink()\n")
        result = self.run_graph()
        self.assertNotEqual(result["status"], "completed", result)
        self.assertIn("CANARY_STARTED", result["graph"]["results"][0]["output"]["stdout"])
        self.assertEqual((self.workspace / "src/value.txt").read_text(), "before\n")

    def test_unrelated_file_content_is_not_readable(self) -> None:
        outside = self.root / "unrelated.txt"
        outside.write_text("fixture-private-value")
        self.script(f"from pathlib import Path\nprint(Path({str(outside)!r}).read_text())\n")
        result = self.run_graph()
        self.assertNotEqual(result["status"], "completed", result)
        self.assertNotIn("fixture-private-value", json.dumps(result))
        self.assertIn("CANARY_STARTED", result["graph"]["results"][0]["output"]["stdout"])

    def test_exact_file_scopes_support_discovery_without_sibling_access(self) -> None:
        source = self.workspace / "src"
        (source / "helper.py").write_text("VALUE = 'allowed-import'\n")
        (source / "unadmitted.txt").write_text("fixture-private-content\n")
        (source / "private").mkdir()
        (source / "private/inside.txt").write_text("nested-private-content\n")
        (source / "alias.txt").symlink_to(source / "unadmitted.txt")
        (source / "test_allowed.py").write_text(
            "import os, unittest\nfrom pathlib import Path\nimport helper\n"
            "class ExactScopeTests(unittest.TestCase):\n"
            "    def test_import_and_allowed_read(self):\n"
            "        self.assertEqual(helper.VALUE, 'allowed-import')\n"
            "        self.assertEqual(Path('src/value.txt').read_text(), 'before\\n')\n"
            "    def test_unadmitted_contents_stay_denied(self):\n"
            "        self.assertIn('unadmitted.txt', os.listdir('src'))\n"
            "        for name in ('src/unadmitted.txt', 'src/alias.txt', 'src/private/inside.txt'):\n"
            "            with self.subTest(name=name), self.assertRaises(PermissionError):\n"
            "                Path(name).read_bytes()\n"
            "    def test_nested_listing_stays_denied(self):\n"
            "        with self.assertRaises(PermissionError): os.listdir('src/private')\n"
            "    def test_writes_and_unlink_stay_denied(self):\n"
            "        with self.assertRaises(PermissionError): Path('src/value.txt').write_text('bad')\n"
            "        with self.assertRaises(PermissionError): Path('src/value.txt').unlink()\n"
        )
        argv = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "src",
                "-p", "test_allowed.py", "-v"]
        contract = self.payload["request"]["jspace"]
        contract.update(read_scopes=["src/value.txt", "src/helper.py", "src/test_allowed.py"],
                        write_scopes=[], allowed_operations=["read", "command"],
                        declared_targets=[],
                        command_templates=[{"argv": argv, "effects": ["read"], "targets": []}])
        self.payload["context"]["surface"]["declared_targets"] = []
        self.payload["request"]["graph"]["commands"][0]["command_line"] = shlex.join(argv)
        self.rehash(self.payload)
        result = self.run_graph()
        self.assertEqual(result["status"], "completed", result)
        output = result["graph"]["results"][0]["output"]
        self.assertIn("Ran 4 tests", output["stderr"])
        self.assertEqual((source / "value.txt").read_text(), "before\n")
        self.assertNotIn("fixture-private-content", json.dumps(result))

    def test_directory_grant_does_not_read_parent_replaced_with_file(self) -> None:
        self.payload["request"]["jspace"]["read_scopes"] = ["src/value.txt"]
        self.payload["request"]["jspace"]["write_scopes"] = []
        profile = sandbox_profile(self.payload["request"], self.settings, self.artifacts)
        (self.workspace / "src/value.txt").unlink()
        (self.workspace / "src").rmdir()
        (self.workspace / "src").write_text("replacement-private-content")
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "/bin/cat", str(self.workspace / "src")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("replacement-private-content", result.stdout)

    def test_parent_directory_rules_preserve_scope_validation(self) -> None:
        contract = self.payload["request"]["jspace"]
        for scopes in ([""], ["../outside/value.txt"]):
            contract["read_scopes"] = scopes
            with self.subTest(scopes=scopes), self.assertRaises(GraphEntryError):
                sandbox_profile(self.payload["request"], self.settings, self.artifacts)
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "foreign").symlink_to(outside, target_is_directory=True)
        contract["read_scopes"] = ["foreign/value.txt"]
        with self.assertRaises(GraphEntryError):
            sandbox_profile(self.payload["request"], self.settings, self.artifacts)
        for scopes in ([], ["."], ["src/**"]):
            contract["read_scopes"] = scopes
            profile = sandbox_profile(self.payload["request"], self.settings, self.artifacts)
            self.assertNotIn("vnode-type DIRECTORY", profile)

    def test_scoped_patch_and_verifier_reuse_actual_core(self) -> None:
        read = self.payload["request"]["graph"]["commands"][0]
        read["step"] = 2
        self.payload["request"]["graph"]["commands"].insert(0, {
            "id": "edit", "step": 1, "command_type": "apply_patch",
            "command_line": "*** Begin Patch\n*** Update File: src/value.txt\n@@\n-before\n+after\n*** End Patch",
        })
        self.payload["request"]["preconditions"] = {
            "edit": {"src/value.txt": hashlib.sha256(b"before\n").hexdigest()}}
        result = self.run_graph()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual((self.workspace / "src/value.txt").read_text(), "after\n")
        self.assertTrue(self.run_graph()["replayed"])

    def test_large_actual_output_is_bounded_and_recoverable(self) -> None:
        self.script("print('x' * 90000)\n")
        result = self.run_graph()
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["output_truncated"])
        self.assertLess(len(json.dumps(result)), 65536)
        import gzip
        archive = Path(result["recovery_artifact"]["path"])
        bundle = json.loads(gzip.decompress(archive.read_bytes()))
        stored = json.loads(bundle["members"][0]["content"])["result"]
        self.assertEqual(canonical_sha256(stored), result["full_result_sha256"])

    def test_cancel_preserves_journal_and_does_not_replay(self) -> None:
        self.payload["request"]["graph"]["commands"][0]["command_line"] = "/bin/sleep 20"
        self.payload["request"]["jspace"]["command_templates"] = [
            {"argv": ["/bin/sleep", "20"], "effects": ["read"], "targets": []}]
        self.rehash(self.payload)

        async def cancel() -> None:
            loop = asyncio.get_running_loop()
            errors = []
            loop.set_exception_handler(lambda _loop, event: errors.append(event))
            task = asyncio.create_task(execute(self.payload, self.settings, self.verify_fixture))
            state = self.artifacts / "tura-graph" / "fixture-task"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                journals = list(state.glob("*.json"))
                if journals and json.loads(journals[0].read_text()).get("nodes"):
                    break
                await asyncio.sleep(.02)
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.fail("engine did not start a node")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(.02)
            self.assertEqual(errors, [])

        asyncio.run(cancel())
        result = self.run_graph()
        self.assertNotEqual(result["status"], "completed", result)
        self.assertIn("GRAPH_EFFECT_UNSETTLED", json.dumps(result))
        journal = next((self.artifacts / "tura-graph" / "fixture-task").glob("*.json"))
        stored = json.loads(journal.read_text())
        self.assertNotEqual(stored["status"], "running")
        text = json.dumps(stored)
        self.assertIn('"process_group_empty": true', text)
        self.assertNotIn('"process_group_empty": false', text)

    def test_abrupt_engine_death_is_unproven_and_not_replayed(self) -> None:
        self.script(
            "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(5)'],"
            " start_new_session=True)\n"
            "Path('src/value.txt').write_text('partial\\n')\n"
            "Path('src/child.json').write_text(json.dumps({'pid':child.pid,'sid':os.getsid(child.pid)}))\n"
            "time.sleep(5)\n"
        )
        template = self.payload["request"]["jspace"]["command_templates"][0]
        template["argv"].extend(["src/value.txt", "src/child.json"])
        template["effects"] = ["read", "modify", "create"]
        template["targets"] = [
            {"argv_index": 3, "operation": "modify", "path": "src/value.txt"},
            {"argv_index": 4, "operation": "create", "path": "src/child.json"},
        ]
        self.payload["request"]["graph"]["commands"][0]["command_line"] = shlex.join(template["argv"])
        self.payload["request"]["jspace"]["declared_targets"].append("src/child.json")
        self.rehash(self.payload)
        self.payload["request"]["graph"]["commands"].append({
            "id": "later-edit", "step": 2, "command_type": "apply_patch",
            "command_line": "*** Begin Patch\n*** Update File: src/value.txt\n@@\n-before\n+after\n*** End Patch",
        })
        self.payload["request"]["preconditions"] = {
            "read": {"src/value.txt": hashlib.sha256(b"before\n").hexdigest(), "src/child.json": None},
            "later-edit": {"src/value.txt": hashlib.sha256(b"before\n").hexdigest()},
        }

        async def kill_engine() -> dict:
            launch = asyncio.create_subprocess_exec
            engines = []

            async def capture(*args, **kwargs):
                process = await launch(*args, **kwargs)
                engines.append(process)
                return process

            with patch("asyncio.create_subprocess_exec", side_effect=capture):
                task = asyncio.create_task(execute(self.payload, self.settings, self.verify_fixture))
                state = self.artifacts / "tura-graph" / "fixture-task"
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if task.done():
                            self.fail(f"engine ended before fault injection: {task.result()}")
                        journals = list(state.glob("*.json"))
                        if (engines and journals and json.loads(journals[0].read_text()).get("nodes")
                                and (self.workspace / "src/child.json").exists()):
                            break
                        await asyncio.sleep(.001)
                    else:
                        self.fail("fixture engine did not record admission")
                    # The outer subprocess is now the supervisor. Kill its exact
                    # direct child (the engine), not the cleanup owner.
                    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                    lib.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
                    lib.proc_listchildpids.restype = ctypes.c_int
                    children = (ctypes.c_int * 16)()
                    count = lib.proc_listchildpids(engines[0].pid, children, ctypes.sizeof(children))
                    self.assertEqual(count, 1)  # This convenience API returns PIDs, not bytes.
                    from codex_collaboration_harness.graph_process import DarwinProcesses
                    child_info = DarwinProcesses().info(children[0])
                    self.assertIsNotNone(child_info)
                    self.assertEqual(child_info.ppid, engines[0].pid)
                    os.kill(children[0], signal.SIGKILL)
                    result = await asyncio.wait_for(task, 8)
                    self.assertEqual(result["process_scope"]["engine_pid"], children[0])
                    return result
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        result = asyncio.run(kill_engine())
        self.assertEqual(result["exit_code"], -signal.SIGKILL, result)
        self.assertEqual(result["effect_state"], "unproven")
        self.assertFalse(result["retry_safe"])
        self.assertTrue(result["process_scope"]["no_live_descendants"], result)
        self.assertTrue(result["process_scope"]["engine_reaped"], result)
        self.assertGreaterEqual(result["process_scope"]["descendant_signals"], 1)
        self.assertEqual((self.workspace / "src/value.txt").read_text(), "partial\n")
        recovery = result["interruption_reconciliation"]
        self.assertEqual(recovery["status"], "recorded", recovery)
        self.assertEqual(recovery["declared_target_observations"][0]["sha256"],
                         hashlib.sha256(b"partial\n").hexdigest())
        self.assertIn("GRAPH_EFFECT_UNSETTLED", json.dumps(self.run_graph()))
        journal = next((self.artifacts / "tura-graph" / "fixture-task").glob("*.json"))
        stored = json.loads(journal.read_text())
        self.assertEqual(stored["status"], "interrupted")
        self.assertEqual(stored["request_digest"], canonical_sha256(self.payload["request"]))
        self.assertTrue(stored["interruption"]["node_states_preserved"])
        self.assertTrue(all(node["status"] == "started" for node in stored["nodes"].values()))
        self.assertEqual((self.workspace / "src/value.txt").read_text(), "partial\n")

    def test_background_session_prevents_success_publication_and_replay(self) -> None:
        self.script(
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(5)'],"
            " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,"
            " start_new_session=True)\nprint('command returned')\n"
        )
        first = self.run_graph()
        self.assertEqual(first["status"], "blocked", first)
        self.assertEqual(first["error"], "GRAPH_DESCENDANTS_OUTLIVED_ENGINE", first)
        self.assertTrue(first["process_scope"]["no_live_descendants"], first)
        self.assertEqual(first["interruption_reconciliation"]["status"], "recorded", first)
        state = self.artifacts / "tura-graph" / "fixture-task"
        self.assertFalse(list(state.glob("*.json.gz")))
        journal = next(state.glob("*.json"))
        before = journal.read_bytes()
        self.assertEqual(json.loads(before)["status"], "interrupted")
        second = self.run_graph()
        self.assertEqual(second["status"], "blocked", second)
        self.assertIn("GRAPH_EFFECT_UNSETTLED", json.dumps(second))
        self.assertEqual(journal.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
