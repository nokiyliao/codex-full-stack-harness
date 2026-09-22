# SPDX-License-Identifier: MIT
"""Prepare bounded context through canonical DCF or explicit local inputs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from . import embedded_nokiy as caller


def _identity(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def verify_action_freshness(workspace: Path, contract: dict) -> None:
    """Use DCF's existing current-domain verifier, not generation age as a proxy."""
    python = workspace / ".venv/bin/python"
    script = (
        "import json,sys;from pathlib import Path;"
        "from scripts.ops.dcf.jspace import verify_contract_freshness;"
        "from scripts.ops.dcf.runtime import DcfRuntime;"
        "verify_contract_freshness(DcfRuntime(Path.cwd()),json.load(sys.stdin))"
    )
    try:
        result = subprocess.run([str(python), "-B", "-c", script], input=json.dumps(contract),
                                cwd=workspace, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_FRESHNESS_UNVERIFIED", "DCF verifier unavailable") from error
    if result.returncode:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_FRESHNESS_UNVERIFIED", "required DCF domains failed verification")


def prepare_request(draft_path: Path, action_path: Path, surface_id: str | None,
                    output: Path) -> dict:
    """Compile once, validate through the real executor, and publish request last."""
    code = "NOKIY_FULL_STACK_PREPARATION_FAILED"
    draft = dict(caller._load_json(draft_path, limit=caller.MAX_REQUEST_BYTES, code=code))
    action = caller._load_json(action_path, limit=caller.MAX_REQUEST_BYTES, code=code)
    if {"context_capsule", "jspace_contract", "request_id", "request_sha256"} & draft.keys():
        raise caller.EmbeddedNokiyError(code, "prepare requires a new draft, not a submitted request")
    if not isinstance(action.get("mission"), dict) or not action["mission"].get("task_id"):
        raise caller.EmbeddedNokiyError(code, "explicit mission/task_id required")
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise caller.EmbeddedNokiyError(code, "new absolute output directory required")
    workspace = caller._plain_path(draft.get("workspace"), name="workspace", directory=True)
    compiler = workspace / "scripts/ops/dcf.py"
    python = workspace / ".venv/bin/python"
    draft.setdefault("schema_version", caller.REQUEST_SCHEMA_VERSION)
    draft.setdefault("execution_profile", "direct")
    draft.setdefault("native_thread_id", os.environ.get("CODEX_THREAD_ID"))
    draft.setdefault("persistence_mode", "native_codex_thread_only")
    if (draft["schema_version"] != caller.REQUEST_SCHEMA_VERSION
            or draft["execution_profile"] not in {"direct", "balanced"}
            or not draft["native_thread_id"]
            or draft["native_thread_id"] != os.environ.get("CODEX_THREAD_ID")):
        raise caller.EmbeddedNokiyError(code, "current native thread and full-core profile required")
    from .local_context import MODE, compile_context, dcf_root
    managed_root = dcf_root(workspace)
    if managed_root is None:
        if surface_id is not None:
            raise caller.EmbeddedNokiyError(code, "DCF surface requested but workspace has no DCF; omit surface for local context")
        capsule, contract = compile_context(workspace, action)
        compiler_before = _identity(Path(__file__).with_name("local_context.py"))
        context_mode = MODE
    else:
        if managed_root != workspace or not compiler.is_file() or not python.is_file():
            raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_DCF_UNAVAILABLE", "use the DCF root and its working interpreter; no local downgrade")
        if not isinstance(surface_id, str) or not surface_id.strip():
            raise caller.EmbeddedNokiyError(code, "DCF-managed workspace requires an exact surface")
        capsule, contract, compiler_before = _compile_dcf(workspace, compiler, python, surface_id, action)
        context_mode = "dcf_jspace_required"
    # A used directory is never reused, even after a failed prepare. Do not erase evidence.
    output.mkdir(mode=0o700)
    caller._write_create_only(output / "capsule.json", capsule)
    caller._write_create_only(output / "jspace.json", contract)
    draft.update(context_capsule=_identity(output / "capsule.json"),
                 jspace_contract=_identity(output / "jspace.json"))
    request = caller.decode_request(draft)
    ready = caller.preflight(request)
    preparation = {
        "status": "PREPARED", "execution_profile": request.execution_profile,
        "context_mode": context_mode, "request_id": request.request_id,
        "compiler": compiler_before, "action": action, "preflight": ready,
        "provider_execution_started": False,
    }
    caller._write_create_only(output / "preparation.json", preparation)
    caller._write_create_only(output / "request.json", request.to_wire(include_identity=False))
    return {**preparation, "request": _identity(output / "request.json")}


def _compile_dcf(workspace, compiler, python, surface_id, action):
    code = "NOKIY_FULL_STACK_PREPARATION_FAILED"
    compiler_before = _identity(compiler)
    try:
        completed = subprocess.run(
            [str(python), "-B", str(compiler), "jspace", "compile", "--surface-id", surface_id,
             "--action-stdin", "--inline", "--json"],
            input=json.dumps(action), capture_output=True, text=True, cwd=workspace, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError(code, "DCF compiler did not finish") from error
    if completed.returncode or len(completed.stdout.encode()) > caller.MAX_REQUEST_BYTES:
        raise caller.EmbeddedNokiyError(code, "DCF compilation failed or response exceeded budget")
    try:
        compiled = json.loads(completed.stdout)
        capsule, contract = compiled["task_context_capsule"], compiled["contract"]
        if not isinstance(capsule, dict) or not isinstance(contract, dict):
            raise ValueError("invalid DCF result")
    except (ValueError, KeyError, TypeError) as error:
        raise caller.EmbeddedNokiyError(code, "DCF did not provide canonical capsule/contract") from error
    if compiler_before != _identity(compiler):
        raise caller.EmbeddedNokiyError(code, "DCF compiler changed during preparation")
    return capsule, contract, compiler_before
