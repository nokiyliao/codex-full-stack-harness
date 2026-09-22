# SPDX-License-Identifier: MIT
"""nokiy: a Codex-owned execution core."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from . import embedded_nokiy as caller
from .graph_process import supervise

MODULE = "codex_collaboration_harness.full_core"
EXECUTOR_NAME = "nokiy"
REQUIRED = {"tura_exec", "tura_runtime", "tura_router", "tura_session_db",
            "provider_config", "direct_config", "direct_prompt", "balanced_config", "balanced_prompt"}
SOURCE_PATHS = {
    "provider_config": "crates/provider/config/provider_config.json",
    "direct_config": "agents/src/direct/agent_config.json",
    "direct_prompt": "agents/src/direct/prompt.md",
    "balanced_config": "agents/src/balanced/agent_config.json",
    "balanced_prompt": "agents/src/balanced/prompt.md",
}


def prepare(request: caller.EmbeddedNokiyRequest):
    caller._verify_native_thread_binding(request)
    if request.schema_version != caller.REQUEST_SCHEMA_VERSION or request.execution_profile not in {"direct", "balanced"}:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_PROFILE_INVALID", "explicit current request required")
    if request.model_provider is not None:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_PROVIDER_OVERRIDE_UNSUPPORTED", "provider is bound by the image")
    if not request.allow_provider_network:
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_PROVIDER_NETWORK_NOT_AUTHORIZED", "provider network required")
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_PROCESS_FENCE_UNAVAILABLE", "Darwin signal isolation required")
    request.codex.verify(code="NOKIY_EMBEDDED_CODEX_DRIFT")
    runtime = caller.verify_runtime_image(request.runtime_image, required_artifacts=REQUIRED)
    for name, relative in SOURCE_PATHS.items():
        if runtime.artifacts[name].path != runtime.runtime_root / relative:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_LAYOUT_INVALID", name)
    # The image is a complete, explicit inventory, not a source-root hint.
    bound = {item.path for item in runtime.artifacts.values()}
    actual = {path for path in runtime.runtime_root.rglob("*") if path.is_file() or path.is_symlink()}
    actual.discard(request.runtime_image.path)
    if actual != bound or any(path.is_symlink() for path in actual):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_IMAGE_INVENTORY_MISMATCH", "unbound or missing image files")
    context, capsule, jspace = caller._verify_context(request)
    tier = request.service_tier or ("priority" if request.model_acceleration else "default")
    ready = {
        "schema_version": caller.PREFLIGHT_SCHEMA_VERSION, "status": "READY",
        "executor_name": EXECUTOR_NAME,
        "request_id": request.request_id, "request_sha256": request.request_sha256,
        "runtime_image_sha256": runtime.image_sha256, "execution_model": "single_task_full_core",
        "execution_profile": request.execution_profile, "model": request.model,
        "reasoning_effort": request.reasoning_effort, "requested_service_tier": tier,
        "observed_model": None, "observed_service_tier": None, "context": context,
        "ephemeral_router": True, "ephemeral_session_db": True, "ephemeral_gateway": False,
        "continuation_owner": "codex", "mission_acceptance": "parent_owned", "fallback_allowed": False,
    }
    return ready, runtime, capsule, jspace


def _environment(request, runtime, state: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("TURA_") or key in {"SESSION_LOG_DB_ROOT", "LOG_PATH"}:
            env.pop(key)
    env.update({
        "TURA_HOME": str(state), "TURA_DB_ROOT": str(state),
        "TURA_PROJECT_ROOT": str(runtime.runtime_root),
        "TURA_PROVIDER_CONFIG": str(runtime.artifacts["provider_config"].path),
        "TURA_GATEWAY_CALLBACKS": "0", "TURA_RUNTIME_AUTO_GIT_COMMIT": "0",
        "TURA_COMMAND_RUN_SANDBOX": "true", "TURA_RUNTIME_ERRORS_FATAL": "1",
        "TURA_SESSION_ACCELERATION_ENABLED": "0", "FORCE_COLOR": "0",
        "PATH": str(runtime.artifacts["tura_exec"].path.parent) + os.pathsep + env.get("PATH", ""),
        "TURA_EXEC_ROUTER_READ_TIMEOUT_SECS": str(request.timeout_seconds),
    })
    return env


def _cli_argv(request, runtime, run: Path, address: str) -> list[str]:
    return [str(runtime.artifacts["tura_exec"].path), "--json", "--sandbox",
            "--router-address", address, "--task-context-capsule", str(run / "capsule.json"),
            "--jspace-contract", str(run / "jspace.json"), "-C", str(request.workspace),
            "--session-id", "full-" + request.request_sha256, "-a", request.execution_profile,
            "-m", "codex/" + request.model, "--model-reasoning-effort", request.reasoning_effort,
            "--service-tier", request.service_tier or ("priority" if request.model_acceleration else "default")]


def _verify_service_owner(name: str, endpoint: dict, process, state: Path) -> None:
    if process.poll() is not None:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_ENDPOINT_OWNER_MISMATCH", name)
    if name == "tura_router":
        if endpoint.get("pid") == process.pid:
            return
    else:
        # Session DB's endpoint has addr/version, not pid. Its existing flock
        # record supplies ownership; requiring a made-up endpoint pid rejects it.
        locks = list((state / ".tura/locks").glob("session-db-*.lock"))
        if len(locks) == 1 and not locks[0].is_symlink():
            with locks[0].open("r+") as lock:
                fields = dict(line.strip().split("=", 1) for line in lock if "=" in line)
                if fields.get("pid") == str(process.pid) and fields.get("kind") == "session_db":
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN)
    raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_ENDPOINT_OWNER_MISMATCH", name)


def _structured_output(item: dict[str, Any]) -> dict[str, Any]:
    output = item.get("aggregated_output")
    if isinstance(output, str):
        try:
            output = json.loads(output, object_pairs_hook=caller._unique_object,
                                parse_constant=caller._invalid_constant)
        except ValueError:
            return {}
    return output if isinstance(output, dict) else {}


def _execution_observation(item: dict[str, Any]) -> bool:
    if item.get("type") not in {"command_execution", "file_change"}:
        return False
    # Full-core JSONL labels task bookkeeping as command_execution too.
    return (item.get("command_type") != "task_status"
            and item.get("command") != "task_status"
            and "task_status" not in _structured_output(item))


def _successful_observation(item: dict[str, Any]) -> bool:
    if item.get("status") != "completed":
        return False
    for result in (item, _structured_output(item)):
        if result.get("success") is False or result.get("isError") is True:
            return False
        code = result.get("exit_code")
        if code is not None and (type(code) is not int or code != 0):
            return False
    return True


def _events(path: Path, limit: int, request: caller.EmbeddedNokiyRequest) -> dict[str, Any]:
    if path.stat().st_size > limit:
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED", "full-core trajectory too large")
    items = [json.loads(line, object_pairs_hook=caller._unique_object,
                        parse_constant=caller._invalid_constant)
             for line in path.read_text().splitlines() if line.strip()]
    terminals = [item for item in items if item.get("type") == "turn.completed"]
    if len(terminals) != 1:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_INVALID", "expected exactly one completed turn")
    terminal = terminals[0]
    if terminal.get("status") != "completed":
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED", "turn not completed")
    expected = {"model": "codex/" + request.model, "agent": request.execution_profile,
                "session_id": "full-" + request.request_sha256, "cwd": str(request.workspace),
                "reasoning_effort": request.reasoning_effort,
                "service_tier": request.service_tier or ("priority" if request.model_acceleration else "default")}
    if any(terminal.get(key) != value for key, value in expected.items()):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_IDENTITY_MISMATCH", "CLI terminal binding differs")
    messages = [item["item"].get("text", "") for item in items
                if item.get("type") == "item.completed" and item.get("item", {}).get("type") == "assistant_message"]
    tools = [item["item"] for item in items if item.get("type") == "item.completed"
             and _execution_observation(item.get("item", {}))]
    successful = [item for item in tools if _successful_observation(item)]
    return {"final_text": messages[-1] if messages else "", "usage": terminal.get("usage"),
            "tool_loop": {"completed_count": len(tools), "successful_count": len(successful)},
            "turn_completed": True}


def _engine(spec_path: Path) -> dict[str, Any]:
    spec = caller._load_json(spec_path, limit=caller.MAX_REQUEST_BYTES, code="NOKIY_FULL_CORE_SPEC_INVALID")
    request = caller.decode_request(spec["request"])
    # Inputs are checked again after entering the isolated process scope.
    _, runtime, _, _ = prepare(request)
    run = spec_path.parent
    state = run / "execution-state"
    state.mkdir(mode=0o700)
    env = _environment(request, runtime, state)
    processes: list[subprocess.Popen] = []
    logs = []
    result: dict[str, Any] = {}
    try:
        address = None
        for name, arguments, marker in (
            ("tura_session_db", [], "service.addr"),
            ("tura_router", ["serve-socket"], "router.addr"),
        ):
            log = (run / (name + ".log")).open("xb")
            logs.append(log)
            process = subprocess.Popen([str(runtime.artifacts[name].path), *arguments],
                                       cwd=request.workspace, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log)
            processes.append(process)
            deadline = time.monotonic() + min(15, request.timeout_seconds)
            endpoint_path = state / "session_log" / marker
            while not endpoint_path.is_file():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_SERVICE_NOT_READY", name)
                time.sleep(.05)
            endpoint = caller._load_json(endpoint_path, limit=16384, code="NOKIY_FULL_CORE_ENDPOINT_INVALID")
            _verify_service_owner(name, endpoint, process, state)
            if name == "tura_router":
                address = endpoint.get("addr")
                if not isinstance(address, str) or not address.startswith("127.0.0.1:"):
                    raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_ENDPOINT_INVALID", name)
                env["TURA_ROUTER_ADDR"] = address
        trajectory = run / "core.jsonl"
        with (run / "prompt.txt").open("rb") as prompt, trajectory.open("xb") as output, (run / "core.stderr").open("xb") as errors:
            process = subprocess.Popen(_cli_argv(request, runtime, run, address),
                                       cwd=request.workspace, env=env, stdin=prompt, stdout=output, stderr=errors)
            processes.append(process)
            while process.poll() is None:
                if trajectory.stat().st_size > request.max_trajectory_bytes or errors.tell() > 4 * 1024 * 1024:
                    raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED", "CLI output budget")
                if any(log.tell() > 4 * 1024 * 1024 for log in logs):
                    raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_SERVICE_LOG_LIMIT", "service log budget")
                time.sleep(.05)
            if process.returncode != 0:
                raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED", "CLI exited unsuccessfully")
        result = _events(trajectory, request.max_trajectory_bytes, request)
    finally:
        # Stop handles we created, never rediscover services by name or persisted PID.
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()
    return result


def execute_full_core(request: caller.EmbeddedNokiyRequest) -> dict[str, Any]:
    caller._verify_native_thread_binding(request)
    run = request.artifact_root / request.request_id
    if run.is_symlink():
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_UNCERTAIN_PRIOR_ATTEMPT", "symlink run directory")
    if (run / "terminal.json").is_file():
        return caller.read_terminal(request.artifact_root, request.request_id)
    ready, runtime, capsule, jspace = prepare(request)
    try:
        run.mkdir(mode=0o700)
    except FileExistsError as error:
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_UNCERTAIN_PRIOR_ATTEMPT", "run exists without terminal") from error
    caller._write_create_only(run / "request-identity.json", request.to_wire())
    caller._write_create_only(run / "capsule.json", capsule)
    caller._write_create_only(run / "jspace.json", jspace)
    caller._write_create_only(run / "execution.json", {"request": request.to_wire(include_identity=False)})
    with (run / "prompt.txt").open("xb") as output:
        output.write(request.prompt.encode())
    # Add a signal-only fence; do not relax inherited filesystem/network policy.
    # Actual tool writes remain under the full core's sandbox and J-Space contract.
    fence = "(version 1) (allow default) (deny signal) (allow signal (target same-sandbox))"
    command = ["/usr/bin/sandbox-exec", "-p", fence, sys.executable, "-B", "-m",
               MODULE, "--supervise", str(run / "execution.json"), "--parent-pid", str(os.getpid())]
    trajectory, errors = run / "supervision.json", run / "supervision.stderr"
    engine, scope = {}, {}
    failure = None
    wall = 0.0
    code = -1
    try:
        code, wall, failure, _ = caller._run_process(command, cwd=request.workspace,
            env=dict(os.environ), stdin_path=run / "prompt.txt", stdout_path=trajectory,
            stderr_path=errors, timeout=request.timeout_seconds + 30, output_limit=request.max_trajectory_bytes)
        outcome = caller._load_json(trajectory, limit=request.max_trajectory_bytes, code="NOKIY_FULL_CORE_SUPERVISION_INVALID")
        engine, scope = outcome.get("result") or {}, outcome.get("scope") or {}
        failure = failure or outcome.get("failure")
        if code != 0:
            failure = failure or "NOKIY_FULL_CORE_EXECUTION_FAILED"
    except (OSError, ValueError, subprocess.SubprocessError, caller.EmbeddedNokiyError) as error:
        failure = failure or getattr(error, "code", "NOKIY_FULL_CORE_SUPERVISION_FAILED")
    cleanup = scope.get("engine_reaped") is True and scope.get("no_live_descendants") is True and not scope.get("cleanup_error")
    if not cleanup:
        failure = failure or "NOKIY_EMBEDDED_CLEANUP_UNPROVEN"
    text = engine.get("final_text") or ""
    if not text:
        failure = failure or "NOKIY_EMBEDDED_RESULT_MISSING"
    if request.require_tool_call and engine.get("tool_loop", {}).get("successful_count", 0) == 0:
        failure = failure or "NOKIY_EMBEDDED_TOOL_LOOP_NOT_OBSERVED"
    preview, truncated = caller._bounded_preview(text, request.max_result_bytes)
    receipt = {
        "schema_version": caller.TERMINAL_SCHEMA_VERSION,
        "executor_name": EXECUTOR_NAME,
        "request_id": request.request_id, "request_sha256": request.request_sha256,
        "status": "BLOCKED" if failure else "RESULT_AVAILABLE", "first_typed_blocker": failure,
        "runtime_image_sha256": runtime.image_sha256, "runtime_build_identity": runtime.build_identity,
        "execution_model": "single_task_full_core", "execution_profile": request.execution_profile,
        "native_thread_id": request.native_thread_id, "continuation_owner": "codex", "mission_acceptance": "parent_owned",
        "model": request.model, "reasoning_effort": request.reasoning_effort,
        "requested_service_tier": ready["requested_service_tier"], "observed_model": None, "observed_service_tier": None,
        "result_text": preview, "result_truncated": truncated, "usage": engine.get("usage"),
        "tool_loop": engine.get("tool_loop"), "wall_time_seconds": wall, "runtime_exit_code": code,
        "cleanup": scope, "cleanup_pass": cleanup, "fallback_used": False,
        "replayable_terminal": True, "preflight_sha256": caller._canonical_sha256(ready),
        "trajectory_artifact": caller._record(run / "core.jsonl") if (run / "core.jsonl").is_file() else None,
        "supervision_artifact": caller._record(trajectory) if trajectory.is_file() else None,
    }
    if len(caller._canonical_bytes(receipt)) > caller.MAX_TERMINAL_BYTES:
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_TERMINAL_TOO_LARGE", "full-core terminal too large")
    caller._write_create_only(run / "terminal.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--engine", type=Path)
    mode.add_argument("--supervise", type=Path)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    if args.engine:
        try:
            print(json.dumps(_engine(args.engine)))
            return 0
        except Exception as error:
            print(json.dumps({"failure": getattr(error, "code", type(error).__name__)}))
            return 2
    spec = caller._load_json(args.supervise, limit=caller.MAX_REQUEST_BYTES, code="NOKIY_FULL_CORE_SPEC_INVALID")
    request = caller.decode_request(spec["request"])
    cancelled = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: cancelled.set())
    try:
        outcome = supervise([sys.executable, "-B", "-m", MODULE, "--engine", str(args.supervise)],
            cwd=str(request.workspace), env=dict(os.environ), input_bytes=b"", timeout=request.timeout_seconds,
            cancelled=cancelled, max_stdout=request.max_trajectory_bytes, max_stderr=65536,
            parent_pid=args.parent_pid)
        result = json.loads(outcome.stdout, object_pairs_hook=caller._unique_object) if outcome.stdout else {}
        failure = outcome.failure or result.get("failure")
        if outcome.returncode != 0:
            failure = failure or "NOKIY_FULL_CORE_ENGINE_FAILED"
        print(json.dumps({"result": result, "scope": outcome.scope, "failure": failure}))
        return 2 if failure else 0
    except Exception as error:
        print(json.dumps({"failure": getattr(error, "code", type(error).__name__), "scope": {}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
