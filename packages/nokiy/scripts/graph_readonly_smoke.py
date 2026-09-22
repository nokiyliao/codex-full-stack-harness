# SPDX-License-Identifier: MIT
"""Verify an installed DCF -> supervised Rust graph entry using source reads only."""

from __future__ import annotations

import argparse
import asyncio
import copy
import gzip
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

from codex_collaboration_harness.graph_entry import DcfBoundary
from codex_collaboration_harness.core import canonical_sha256


def verify_process_closeout(result: dict) -> dict:
    scope = result.get("process_scope", {})
    scopes = [scope]
    if not result.get("replayed"):
        scopes.append(scope.get("finalization_process_scope", {}))
    for observed in scopes:
        if (observed.get("scope") != "per-call-inherited-seatbelt-signal-boundary"
                or observed.get("engine_reaped") is not True
                or observed.get("no_live_descendants") is not True
                or observed.get("cleanup_error", "missing") is not None
                or observed.get("engine_exit_code") != 0
                or observed.get("descendant_signals") != 0
                or observed.get("effect_outcome_inferred_from_process_exit") is not False):
            raise ValueError("GRAPH_SMOKE_PROCESS_CLOSEOUT_UNPROVEN")
    if result.get("process_closeout_required") is not False:
        raise ValueError("GRAPH_SMOKE_FINALIZATION_INCOMPLETE")
    archive = Path(result["recovery_artifact"]["path"])
    bundle = json.loads(gzip.decompress(archive.read_bytes()))
    journal = json.loads(bundle["members"][0]["content"])
    if journal.get("supervision_version") != 1 or not journal.get("process_scope"):
        raise ValueError("GRAPH_SMOKE_ARCHIVED_PROCESS_SCOPE_MISSING")
    return {"status": "PASS_SUPERVISED_CLOSEOUT", "process_scope": scope,
            "archived_supervision_version": journal["supervision_version"]}


def verify_terminal_retention(result: dict, artifacts: Path, task_id: str) -> dict:
    reference = result.get("recovery_artifact", {})
    if reference.get("format") != "gzip-terminal-bundle-v1":
        raise ValueError("GRAPH_TERMINAL_BUNDLE_REQUIRED")
    root = artifacts / "tura-graph" / task_id
    key = canonical_sha256([task_id, result["call_id"]])
    archive = root / (key + ".json.gz")
    if reference.get("path") != str(archive) or archive.is_symlink():
        raise ValueError("GRAPH_TERMINAL_BUNDLE_PATH_MISMATCH")
    compressed = archive.read_bytes()
    bundle = json.loads(gzip.decompress(compressed))
    if bundle.get("semantic_sha256") != canonical_sha256({
        k: v for k, v in bundle.items() if k != "semantic_sha256"
    }):
        raise ValueError("GRAPH_TERMINAL_BUNDLE_DIGEST_MISMATCH")
    members = bundle["members"]
    journal = json.loads(members[0]["content"])
    guard_path = root / (key + ".json")
    if (guard_path.is_symlink() or not guard_path.is_file()
            or guard_path.stat().st_nlink != 1):
        raise ValueError("GRAPH_ROLLBACK_GUARD_IDENTITY_INVALID")
    guard_bytes = guard_path.read_bytes()
    guard = json.loads(guard_bytes)
    expected_guard = {
        "schema_version": "codex_tura_graph_journal_v1", "closeout_version": 1,
        "task_id": journal["task_id"], "call_id": journal["call_id"],
        "request_digest": journal["request_digest"], "status": "sealed",
        "archive_semantic_sha256": bundle["semantic_sha256"],
        "recovery_artifact": {"format": "gzip-terminal-bundle-v1", "path": str(archive)},
    }
    if guard != expected_guard:
        raise ValueError("GRAPH_ROLLBACK_GUARD_MISMATCH")
    for index, member in enumerate(members):
        path = Path(member["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("GRAPH_TERMINAL_MEMBER_PATH_INVALID")
        content = member["content"].encode()
        if (len(content) != member["bytes"]
                or hashlib.sha256(content).hexdigest() != member["sha256"]
                or (index == 0 and root / path != guard_path)
                or (index > 0 and ((root / path).exists() or (root / path).is_symlink()))):
            raise ValueError("GRAPH_TERMINAL_MEMBER_NOT_RECLAIMED_OR_INVALID")
    if (journal["result_digest"] != canonical_sha256(journal["result"])
            or journal["task_id"] != task_id or journal["call_id"] != result["call_id"]
            or journal["request_digest"] != result["request_digest"]
            or archive.with_suffix(".gz.tmp").exists() or guard_path.with_suffix(".tmp").exists()
            or archive.stat().st_nlink != 1):
        raise ValueError("GRAPH_TERMINAL_RETENTION_READBACK_FAILED")
    raw_bytes = sum(member["bytes"] for member in members)
    retained_bytes = len(compressed) + len(guard_bytes)
    return {"status": "PASS_EXACT_CALL_TERMINAL_RETENTION", "archive_path": str(archive),
            "archive_sha256": hashlib.sha256(compressed).hexdigest(),
            "rollback_guard_path": str(guard_path),
            "rollback_guard_sha256": hashlib.sha256(guard_bytes).hexdigest(),
            "original_files": len(members), "retained_files": 2,
            "original_bytes": raw_bytes, "retained_bytes": retained_bytes,
            "net_reclaimed_logical_bytes": raw_bytes - retained_bytes,
            "exact_reconstruction_verified": True, "duplicate_members_remaining": 0}


async def invoke_installed(entry: Path, payload: dict, task_id: str,
                           mode: str = "invoke", expected_status: str = "completed") -> dict:
    if os.environ.get("CODEX_THREAD_ID") != task_id:
        raise ValueError("GRAPH_INSTALLED_SMOKE_REQUIRES_NATIVE_CALLER")
    process = await asyncio.create_subprocess_exec(
        str(entry), mode,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home()),
             "CODEX_THREAD_ID": task_id, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode()), 170,
        )
    except BaseException:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    expected_code = 3 if expected_status == "native_required" else 0
    if process.returncode != expected_code:
        raise ValueError(f"GRAPH_INSTALLED_SMOKE_FAILED:{stdout[:4096]!r}:"
                         f"stderr_sha256={hashlib.sha256(stderr).hexdigest()}")
    result = json.loads(stdout)
    expected_host = {"execute": "native-shell-task-bound-direct-graph",
                     "invoke": "native-shell-task-bound-stdio-mcp",
                     "auto": "native-shell-task-bound-auto-selection"}[mode]
    if (result.get("status") != expected_status
            or result.get("host_invocation") != expected_host
            or result.get("direct_native_mcp_registration") is not False):
        raise ValueError("GRAPH_INSTALLED_SMOKE_WRONG_INVOCATION")
    if mode in {"execute", "auto"} and result.get("mcp_transport_used") is not False:
        raise ValueError("GRAPH_INSTALLED_SMOKE_UNEXPECTED_MCP_TRANSPORT")
    return result


async def run(args: argparse.Namespace) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo = args.dcf_root.resolve(strict=True)
    engine = args.engine.resolve(strict=True)
    artifacts = args.artifact_root.resolve(strict=True)
    task_id = args.task_id or os.environ.get("CODEX_THREAD_ID")
    if not task_id:
        raise ValueError("GRAPH_CALLER_TASK_ID_REQUIRED")
    source = "scripts/ops/dcf/jspace.py"
    source_before = hashlib.sha256((repo / source).read_bytes()).hexdigest()
    engine_sha = hashlib.sha256(engine.read_bytes()).hexdigest()
    boundary = DcfBoundary(repo)
    action = {
        "repo_root": str(repo), "operations": ["read", "command"],
        "read_scopes": [source], "write_scopes": [], "declared_targets": [source],
        "denied_operations": ["network", "install", "delete", "system_mutation"],
        "command_templates": [
            {"argv": ["/usr/bin/printf", source], "effects": ["read"], "targets": []},
            {"argv": ["/usr/bin/wc", "-c", source], "effects": ["read"],
             "targets": [{"operation": "read", "path": source, "argv_index": 2}]},
        ],
        "mission": {"task_id": task_id, "mission_id": "tura-graph-integration",
                    "mode": "DELIVERY", "current_predicate": "same-task-source-readback",
                    "objective": "Verify only the DCF source byte count through the actual graph tool."},
        "context_summary": "Read-only integration check; no runtime, broker, account or market data.",
    }
    contract = boundary.jspace.compile_jspace_contract(
        boundary.runtime, surface_id="research_execution_engine", action=action,
    )
    context = boundary.jspace.compile_task_context_capsule(contract, action=action)
    boundary.verify(contract)
    request = {
        "schema_version": "codex_tura_graph_v1", "task_id": task_id,
        "call_id": "source-read-" + uuid.uuid4().hex,
        "workspace": str(repo), "expires_at_unix": int(time.time()) + 120,
        "timeout_ms": 5000, "jspace": contract, "preconditions": {},
        "graph": {"commands": [
            {"id": "path", "step": 1, "command_type": "shell_command",
             "command_line": "/usr/bin/printf " + source},
            {"id": "bytes", "step": 2, "command_type": "shell_command",
             "command_line": "/usr/bin/wc -c '#@#${path.stdout}#@#$'"},
        ]},
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "codex_collaboration_harness.graph_entry", "serve",
              "--engine", str(engine), "--engine-sha256", engine_sha,
              "--dcf-root", str(repo), "--artifact-root", str(artifacts),
              "--caller-task-id", task_id],
        env={"PATH": "/usr/bin:/bin", "HOME": str(artifacts),
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
             "PYTHONDONTWRITEBYTECODE": "1"},
    )
    started = time.monotonic()
    payload = {"request": request, "context": context}
    native_selection = None
    if args.installed_entry and args.entry_mode == "auto":
        native_payload = copy.deepcopy(payload)
        native_request = native_payload["request"]
        native_request["call_id"] = "native-select-" + uuid.uuid4().hex
        native_request["graph"] = {"commands": [{
            "id": "bytes", "step": 1, "command_type": "shell_command",
            "command_line": "/usr/bin/wc -c " + source,
        }]}
        native_key = canonical_sha256([task_id, native_request["call_id"]])
        state = artifacts / "tura-graph" / task_id
        if list(state.glob(native_key + "*")):
            raise ValueError("GRAPH_AUTO_NATIVE_PROBE_IDENTITY_ALREADY_PRESENT")
        selected = await invoke_installed(args.installed_entry, native_payload, task_id,
                                           "auto", "native_required")
        if (selected.get("task_id") != task_id
                or selected.get("call_id") != native_request["call_id"]
                or selected.get("request_digest") != canonical_sha256(native_request)
                or selected.get("execution_started") is not False
                or selected.get("call_identity_reserved") is not False
                or selected.get("requires_native_tool_call") is not True
                or selected.get("route_selection", {}).get("selected_route") != "native"
                or selected.get("route_selection", {}).get("authorization_granted") is not False
                or list(state.glob(native_key + "*"))):
            raise ValueError("GRAPH_AUTO_NATIVE_RECOMMENDATION_NOT_PROVEN")
        native_selection = {"status": "PASS_NON_EXECUTING_NATIVE_RECOMMENDATION",
                            "call_id": native_request["call_id"], "cli_exit_code": 3,
                            "call_artifacts_created": 0, "native_tool_execution_proven": False}
    if args.installed_entry:
        result = await invoke_installed(args.installed_entry, payload, task_id, args.entry_mode)
        retained_before_replay = verify_terminal_retention(result, artifacts, task_id)
        repeat_result = await invoke_installed(args.installed_entry, payload, task_id, args.entry_mode)
        if repeat_result.get("replayed") is not True:
            raise ValueError("GRAPH_REPLAY_SMOKE_FAILED")
        if args.entry_mode == "auto":
            for observed in (result, repeat_result):
                if observed.get("route_selection", {}).get("selected_route") != "graph":
                    raise ValueError("GRAPH_AUTO_GRAPH_SELECTION_NOT_PROVEN")
            if repeat_result["route_selection"]["reason"] != "existing_call_requires_graph_recovery":
                raise ValueError("GRAPH_AUTO_REPLAY_ROUTE_CHANGED")
        names = ["nokiy_graph_execute"] if args.entry_mode == "invoke" else []
    else:
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = await client.list_tools()
                names = [tool.name for tool in tools.tools]
                if names != ["nokiy_graph_execute"]:
                    raise ValueError("GRAPH_TOOL_INVENTORY_MISMATCH")
                answer = await client.call_tool("nokiy_graph_execute", payload)
                result = answer.structuredContent
                if answer.isError or not isinstance(result, dict) or result.get("status") != "completed":
                    raise ValueError(f"GRAPH_MCP_SMOKE_FAILED:{result}")
                retained_before_replay = verify_terminal_retention(result, artifacts, task_id)
                repeat = await client.call_tool("nokiy_graph_execute", payload)
                repeat_result = repeat.structuredContent
                if repeat.isError or repeat_result.get("replayed") is not True:
                    raise ValueError("GRAPH_REPLAY_SMOKE_FAILED")
    if result.get("replayed") is not False:
        raise ValueError("GRAPH_FIRST_EXECUTION_WAS_NOT_FRESH")
    for observed_result in (result, repeat_result):
        if (observed_result.get("task_id") != task_id
                or observed_result.get("call_id") != request["call_id"]):
            raise ValueError("GRAPH_SMOKE_IDENTITY_MISMATCH")
    if args.installed_entry:
        if repeat_result.get("request_digest") != result.get("request_digest"):
            raise ValueError("GRAPH_REPLAY_SMOKE_FAILED")
    source_after = hashlib.sha256((repo / source).read_bytes()).hexdigest()
    if source_after != source_before:
        raise ValueError("GRAPH_READONLY_SOURCE_CHANGED")
    rows = result["graph"]["results"]
    observed = rows[-1]["output"]["stdout"].split()
    if int(observed[0]) != (repo / source).stat().st_size:
        raise ValueError("GRAPH_SOURCE_BYTE_COUNT_MISMATCH")
    retained_after_replay = verify_terminal_retention(result, artifacts, task_id)
    if retained_after_replay != retained_before_replay:
        raise ValueError("GRAPH_TERMINAL_ARCHIVE_CHANGED_ON_REPLAY")
    process_closeout = None
    if args.require_supervised:
        process_closeout = {
            "first_execution": verify_process_closeout(result),
            "cached_replay": verify_process_closeout(repeat_result),
        }
    return {
        "status": ({"execute": "PASS_DCF_DIRECT_RUST_SOURCE_READBACK",
                    "auto": "PASS_DCF_AUTO_RUST_SOURCE_READBACK",
                    "invoke": "PASS_DCF_MCP_RUST_SOURCE_READBACK"}[args.entry_mode]
                   if args.installed_entry else "PASS_DCF_MCP_RUST_SOURCE_READBACK"),
        "task_id": task_id, "call_id": request["call_id"],
        "engine_sha256": engine_sha,
        "dcf_generation_id": contract["dcf_generation"]["generation_id"],
        "context_semantic_sha256": context["semantic_sha256"],
        "source_sha256": source_after, "source_bytes": int(observed[0]),
        "tool_names": names, "actual_engine": result["engine"],
        "completed_nodes": len(rows), "replay_without_execution": True,
        "source_unchanged": True,
        "wall_seconds_including_entry_start_and_replay": round(time.monotonic() - started, 6),
        "installed_entry": str(args.installed_entry) if args.installed_entry else None,
        "host_invocation": result.get("host_invocation", "standalone-stdio-client"),
        "native_host_discovery": "not_direct_native_mcp_registration",
        "effects": "task-local-recovery-artifacts-only",
        "process_closeout": process_closeout,
        "native_selection": native_selection,
        "terminal_retention": {**retained_after_replay, "archive_unchanged_on_replay": True},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dcf-root", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--installed-entry", type=Path)
    parser.add_argument("--entry-mode", choices=("auto", "execute", "invoke"), default="execute")
    parser.add_argument("--require-supervised", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
