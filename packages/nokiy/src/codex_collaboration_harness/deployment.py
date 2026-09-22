"""Exact parent-admitted deployment actions; no model or deployment authority issuer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import threading
import time
from typing import Any

from . import embedded_nokiy as caller

SCHEMA = "nokiy_deployment_plan_v1"
TERMINAL = "nokiy_deployment_terminal_v1"
STAGES = ("preflight", "apply", "verify")
LIMIT = 128 * 1024
ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")


def fail(code: str) -> None:
    raise caller.EmbeddedNokiyError("NOKIY_DEPLOYMENT_" + code, code)


def document(path: Path) -> dict[str, Any]:
    return caller._load_json(path, limit=LIMIT, code="NOKIY_DEPLOYMENT_INVALID_JSON")


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(path: Path, value: dict[str, Any]) -> None:
    caller._write_create_only(path, value)
    sync_directory(path.parent)


def absolute(value: Any, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        fail("ABSOLUTE_PATH_REQUIRED")
    path = Path(value)
    if path.resolve(strict=True) != path or (directory and not path.is_dir()):
        fail("CANONICAL_PATH_REQUIRED")
    return path


def identity(value: Any) -> Path:
    if not isinstance(value, dict) or set(value) not in ({"path", "sha256"}, {"path", "sha256", "realpath"}):
        fail("FILE_IDENTITY_REQUIRED")
    if not isinstance(value["path"], str) or not Path(value["path"]).is_absolute():
        fail("ABSOLUTE_PATH_REQUIRED")
    path = Path(value["path"])
    resolved = path.resolve(strict=True)
    if (resolved != path or "realpath" in value) and value.get("realpath") != str(resolved):
        fail("RESOLVED_IDENTITY_REQUIRED")
    if not path.is_file() or caller._file_sha256(path) != value["sha256"]:
        fail("IDENTITY_DRIFT")
    return path


def load(path: Path, approved: str) -> tuple[dict[str, Any], str]:
    plan = document(path)
    digest = caller._canonical_sha256(plan)
    if digest != approved:
        fail("APPROVAL_DIGEST_MISMATCH")
    if set(plan) != {"schema_version", "action_id", "native_thread_id", "workspace",
                     "artifact_root", "target", "release", "expires_at", "authorization_ref", "commands"}:
        fail("PLAN_FIELDS_INVALID")
    if plan["schema_version"] != SCHEMA or not isinstance(plan["action_id"], str) or not ID.fullmatch(plan["action_id"]):
        fail("PLAN_ID_INVALID")
    thread = plan["native_thread_id"]
    if not isinstance(thread, str) or not caller.THREAD_ID_PATTERN.fullmatch(thread) or thread != os.environ.get("CODEX_THREAD_ID"):
        fail("PARENT_THREAD_MISMATCH")
    for field in ("target", "release"):
        if not isinstance(plan[field], str) or not plan[field].strip() or len(plan[field]) > 256:
            fail("TARGET_BINDING_REQUIRED")
    absolute(plan["workspace"], directory=True)
    absolute(plan["artifact_root"], directory=True)
    if not isinstance(plan["commands"], dict) or set(plan["commands"]) != set(STAGES):
        fail("THREE_STAGES_REQUIRED")
    if type(plan["expires_at"]) not in (int, float):
        fail("EXPIRY_REQUIRED")
    for spec in plan["commands"].values():
        if not isinstance(spec, dict) or set(spec) != {"argv", "files", "timeout_seconds"}:
            fail("COMMAND_INVALID")
        argv = spec["argv"]
        if not isinstance(argv, list) or not 1 <= len(argv) <= 64 or any(not isinstance(a, str) or not a or "\0" in a for a in argv):
            fail("ARGV_INVALID")
        if type(spec["timeout_seconds"]) is not int or not 1 <= spec["timeout_seconds"] <= 3600:
            fail("TIMEOUT_INVALID")
        if not isinstance(spec["files"], list) or not 1 <= len(spec["files"]) <= 64:
            fail("IDENTITIES_REQUIRED")
        # Admit an executable entrypoint or a pinned script, not inline shell/code.
        paths = {f.get("path") for f in spec["files"] if isinstance(f, dict)}
        if argv[0] not in paths or any(a in {"-c", "-m", "-e", "--eval", "--command"} for a in argv[1:]):
            fail("PINNED_ENTRYPOINT_REQUIRED")
        if Path(argv[0]).name.startswith(("python", "node", "ruby", "perl", "bash", "zsh", "sh")):
            operands = [a for a in argv[1:] if not a.startswith("-")]
            if not operands or operands[0] not in paths:
                fail("PINNED_SCRIPT_REQUIRED")
    return plan, digest


def fresh(plan: dict[str, Any]) -> None:
    if time.time() >= plan["expires_at"]:
        fail("AUTHORIZATION_EXPIRED")
    identity(plan["authorization_ref"])
    for spec in plan["commands"].values():
        for item in spec["files"]:
            identity(item)
        if not os.access(spec["argv"][0], os.X_OK):
            fail("ENTRYPOINT_NOT_EXECUTABLE")


def clean(scope: dict[str, Any]) -> bool:
    return (
        scope.get("process_stopped") is True
        and not scope.get("unsettled_descendant_pids")
    )


def binding(plan: dict[str, Any], digest: str) -> dict[str, Any]:
    return {"action_id": plan["action_id"], "target": plan["target"],
            "release": plan["release"], "plan_sha256": digest}


def run_stages(plan: dict[str, Any], digest: str, run: Path) -> dict[str, Any]:
    cancelled = threading.Event()
    previous_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, lambda _sig, _frame: cancelled.set())
    rows: list[dict[str, Any]] = []
    attempted = False
    blocker = None
    try:
        for stage in STAGES:
            fresh(plan)
            if cancelled.is_set():
                fail("CANCELLED")
            spec = plan["commands"][stage]
            save(run / (stage + ".started.json"), binding(plan, digest))
            attempted = attempted or stage == "apply"
            env = dict(os.environ)
            env.update(NOKIY_DEPLOYMENT_PLAN_SHA256=digest, NOKIY_DEPLOYMENT_ACTION_ID=plan["action_id"])
            stdout_path = run / (stage + ".stdout")
            stderr_path = run / (stage + ".stderr")
            returncode, wall_time, failure, cleanup = caller._run_process(
                spec["argv"], cwd=Path(plan["workspace"]), env=env,
                stdin_path=Path(os.devnull), stdout_path=stdout_path,
                stderr_path=stderr_path, timeout=spec["timeout_seconds"],
                output_limit=LIMIT,
            )
            scope = {
                "scope": "parent-task-process-group",
                "process_stopped": cleanup.get("process_stopped") is True,
                "observed_descendant_pids": cleanup.get("observed_descendant_pids", []),
                "unsettled_descendant_pids": cleanup.get("unsettled_descendant_pids", []),
                "sandbox_applied": False,
                "wall_time_seconds": wall_time,
            }
            row = {"stage": stage, "returncode": returncode, "scope": scope, "failure": failure}
            rows.append(row)
            save(run / (stage + ".result.json"), row)
            if failure or returncode != 0 or not clean(scope):
                blocker = failure
                fail(stage.upper() + "_FAILED")
            if stage != "apply":
                attestation = json.loads(stdout_path.read_bytes(), object_pairs_hook=caller._unique_object,
                                         parse_constant=caller._invalid_constant)
                expected = binding(plan, digest)
                expected.update({"admitted": True} if stage == "preflight" else
                                {"verified": True, "healthy": True, "observed_release": plan["release"]})
                if not isinstance(attestation, dict) or any(type(attestation.get(k)) is not type(v) or attestation.get(k) != v for k, v in expected.items()):
                    fail(stage.upper() + "_ATTESTATION_MISMATCH")
    except Exception as error:
        blocker = blocker or getattr(error, "code", "NOKIY_DEPLOYMENT_STAGE_ERROR")
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return {"stages": rows, "apply_attempted": attempted, "first_typed_blocker": blocker,
            "cleanup_pass": bool(rows) and all(clean(row["scope"]) for row in rows)}


def read_result(root: Path, action_id: str) -> dict[str, Any]:
    if not ID.fullmatch(action_id):
        fail("PLAN_ID_INVALID")
    run = absolute(str(root), directory=True) / ("deployment-" + action_id)
    absolute(str(run), directory=True)
    result = document(run / "terminal.json")
    saved = document(run / "plan.json")
    if (result.get("schema_version") != TERMINAL or saved.get("action_id") != action_id
            or any(result.get(k) != v for k, v in binding(saved, caller._canonical_sha256(saved)).items())
            or result.get("native_thread_id") != saved.get("native_thread_id")
            or result.get("native_thread_id") != os.environ.get("CODEX_THREAD_ID")):
        fail("RESULT_IDENTITY_MISMATCH")
    return result


def execute(path: Path, approved: str, *, check_only: bool = False) -> dict[str, Any]:
    plan, digest = load(path, approved)
    run = Path(plan["artifact_root"]) / ("deployment-" + plan["action_id"])
    if run.is_symlink():
        fail("UNSAFE_RESULT_PATH")
    if run.exists():
        if caller._canonical_sha256(document(run / "plan.json")) != digest:
            fail("ACTION_ID_CONFLICT")
        if (run / "terminal.json").is_file():
            return read_result(Path(plan["artifact_root"]), plan["action_id"])
        fail("UNCERTAIN_PRIOR_ATTEMPT")
    fresh(plan)
    if check_only:
        return {"status": "READY", **binding(plan, digest), "execution_started": False,
                "domain_admission_checked": False}
    try:
        run.mkdir(mode=0o700)
    except FileExistsError:
        fail("UNCERTAIN_PRIOR_ATTEMPT")
    sync_directory(run.parent)
    save(run / "plan.json", plan)
    result: dict[str, Any] = {}
    blocker = None
    try:
        result = run_stages(plan, digest, run)
        save(run / "supervision.json", result)
        blocker = result.get("first_typed_blocker")
        if not result.get("cleanup_pass"):
            blocker = blocker or "NOKIY_DEPLOYMENT_SUPERVISION_FAILED"
        if [r.get("stage") for r in result.get("stages", [])] != list(STAGES):
            blocker = blocker or "NOKIY_DEPLOYMENT_INCOMPLETE"
    except Exception as error:
        blocker = blocker or getattr(error, "code", "NOKIY_DEPLOYMENT_SUPERVISION_LOST")
    attempted = (run / "apply.started.json").exists()
    # Missing supervision may hide an effect: never advertise it as pre-execution.
    terminal = {"schema_version": TERMINAL, **binding(plan, digest), "native_thread_id": plan["native_thread_id"],
        "status": "VERIFIED" if not blocker else "EFFECT_UNCERTAIN" if attempted or not result else "BLOCKED_BEFORE_APPLY",
        "first_typed_blocker": blocker, "apply_attempted": attempted,
        "cleanup_pass": result.get("cleanup_pass", False), "stages": result.get("stages", []),
        "execution_model": "exact_parent_admitted_commands_direct", "provider_execution_started": False,
        "execution_boundary": "parent_task_environment_no_added_seatbelt",
        "mission_acceptance": "parent_owned", "automatic_retry": False}
    try:
        save(run / "terminal.json", terminal)
    except OSError:
        terminal.update(status="EFFECT_UNCERTAIN", first_typed_blocker="NOKIY_DEPLOYMENT_TERMINAL_NOT_DURABLE")
    return terminal
