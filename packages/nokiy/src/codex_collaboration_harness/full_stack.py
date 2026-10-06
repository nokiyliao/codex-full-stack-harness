# SPDX-License-Identifier: MIT
"""Prepare bounded context through canonical DCF or explicit local inputs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from . import embedded_nokiy as caller


_NAVIGATION_TARGET = re.compile(r"^symbol:[A-Za-z_][A-Za-z0-9_.]{0,254}$")
_NAVIGATION_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")
_SURFACE_UNVERIFIED = "NOKIY_FULL_STACK_SURFACE_UNVERIFIED"

# The only path matching and catalog read happen inside the project's own DCF
# interpreter. Do not route paths via sourcegraph or a partial public preview.
_SURFACE_QUERY = """
import json, sys
from pathlib import Path
from scripts.ops.dcf.collectors import path_matches_surface
from scripts.ops.dcf.query import query_capability, _semantic_sha256
from scripts.ops.dcf.runtime import DcfRuntime

paths = json.load(sys.stdin)
response, _ = query_capability(DcfRuntime(Path.cwd()), capability_id="surface-map",
                               full_output=True)
wire = response.model_dump(mode="json")
if (response.freshness_status != "current" or response.projection_status != "pass"
        or response.domain_verdict != "pass"):
    raise ValueError("surface-map is not current and passing")
rows = wire["result"]["surfaces"]
if not isinstance(rows, list) or len(rows) > 4096:
    raise ValueError("invalid surface-map catalog")
ids = [row["surface_id"] for row in rows]
if any(not isinstance(sid, str) or not sid or len(sid) > 255 for sid in ids) or len(set(ids)) != len(ids):
    raise ValueError("invalid surface IDs")
matches = [[sid for sid, row in zip(ids, rows) if path_matches_surface(path, row)] for path in paths]
wire["result"] = {"surface_count": len(rows), "full_result_sha256": _semantic_sha256(response.result),
                  "paths": paths, "matches": matches, "complete": True}
payload = json.dumps(wire, sort_keys=True)
if len(payload.encode("utf-8")) > 65536:
    raise ValueError("surface-map evidence exceeds response budget")
sys.stdout.write(payload)
"""


def _surface_file_identity(workspace: Path, name: str) -> tuple[int, int, int]:
    code = "NOKIY_FULL_STACK_SURFACE_TARGET_INVALID"
    if (not isinstance(name, str) or not name or len(name) > 512
            or Path(name).is_absolute() or Path(name).as_posix() != name
            or any(c in name for c in "*?[]{}\\\x00\r\n")
            or any(part in {"", ".", "..", ".git"} for part in name.split("/"))):
        raise caller.EmbeddedNokiyError(code, "surface_targets require exact normalized workspace-relative files")
    node = workspace
    try:
        parts = name.split("/")
        for i, part in enumerate(parts):
            node = node / part
            info = node.lstat()
            if not (stat.S_ISREG(info.st_mode) if i == len(parts) - 1 else stat.S_ISDIR(info.st_mode)):
                raise ValueError("non-regular file or symlink component")
    except (OSError, ValueError) as error:
        raise caller.EmbeddedNokiyError(code, "surface target is not a physical regular file under workspace") from error
    return info.st_dev, info.st_ino, info.st_mode


def _surface_files_still_current(workspace: Path, identities: dict) -> bool:
    try:
        return all(_surface_file_identity(workspace, path) == identity
                   for path, identity in identities.items())
    except caller.EmbeddedNokiyError:
        return False


def _resolve_surface_targets(workspace: Path, python: Path, action: dict,
                             surface_id: str | None) -> tuple[dict, str, dict, dict]:
    targets = action["surface_targets"]
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 4
            or len(set(t for t in targets if isinstance(t, str))) != len(targets)):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_TARGET_INVALID",
                                         "surface_targets require 1..4 distinct paths")
    identities = {}
    for target in targets:
        identity = _surface_file_identity(workspace, target)
        identities[target] = identity
    if len(set(identities.values())) != len(targets):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_TARGET_INVALID",
                                         "surface_targets must identify distinct physical files")
    try:
        completed = subprocess.run([str(python), "-B", "-c", _SURFACE_QUERY],
                                   input=json.dumps(targets), cwd=workspace, capture_output=True,
                                   text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED, "surface-map query did not finish") from error
    if (completed.returncode or len(completed.stdout.encode("utf-8")) > 65536
            or len(completed.stderr.encode("utf-8")) > 4096):
        raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED, "surface-map query failed or exceeded budget")
    try:
        wire = json.loads(completed.stdout)
        result = wire["result"]
        matches = result["matches"]
        count = result["surface_count"]
        if (wire["schema_version"] != "dcf_query_response_v2"
                or wire["capability_id"] != "surface-map" or wire["target"] is not None
                or wire["depth"] != 2 or wire["freshness_status"] != "current"
                or wire["projection_status"] != "pass" or wire["domain_verdict"] != "pass"
                or wire["safety"]["authority_effect"] != "none"
                or wire["safety"]["no_apply"] is not True
                or wire["safety"]["protected_mutation_authorized"] is not False
                or wire["safety"]["command_queue_authority"] != "proposal_only"
                or not isinstance(wire["generation_id"], str) or not wire["generation_id"]
                or set(result) != {"surface_count", "full_result_sha256", "paths", "matches", "complete"}
                or result["complete"] is not True or result["paths"] != targets
                or type(count) is not int or not 0 <= count <= 4096
                or not re.fullmatch(r"[0-9a-f]{64}", result["full_result_sha256"])
                or not isinstance(matches, list) or len(matches) != len(targets)):
            raise ValueError("incomplete or stale surface-map response")
        for row in matches:
            if (not isinstance(row, list) or len(row) > count
                    or any(not isinstance(sid, str) or not sid or len(sid) > 255 for sid in row)
                    or len(set(row)) != len(row)):
                raise ValueError("invalid surface candidates")
        if not _surface_files_still_current(workspace, identities):
            raise ValueError("target changed during surface lookup")
    except (KeyError, TypeError, ValueError, caller.EmbeddedNokiyError) as error:
        raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED, "surface-map evidence is not current and complete") from error
    common = set.intersection(*(set(row) for row in matches))
    if not common:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_UNRESOLVED",
                                         "no common registered surface for all targets")
    if surface_id is None:
        if len(common) != 1:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_AMBIGUOUS",
                                             "multiple common surfaces; supply an exact surface ID")
        surface_id = next(iter(common))
    elif surface_id not in common:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_MISMATCH",
                                         "explicit surface ID does not match every target")
    compiled_action = dict(action)
    compiled_action.pop("surface_targets")
    evidence = {"generation_id": wire["generation_id"], "surface_map_sha256": result["full_result_sha256"],
                "surface_count": count, "targets": targets,
                "candidates": [sorted(row) for row in matches], "common_surface_ids": sorted(common),
                "selected_surface_id": surface_id}
    return compiled_action, surface_id, evidence, identities


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


def _query_navigation(workspace: Path, argv: list[str], target: str) -> dict:
    code = "NOKIY_FULL_STACK_NAVIGATION_UNVERIFIED"
    try:
        response = subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError(code, "DCF navigation did not finish") from error
    if response.returncode or len(response.stdout.encode()) > 65536:
        raise caller.EmbeddedNokiyError(code, "DCF navigation failed or exceeded its response budget")
    payload = json.loads(response.stdout)
    if (payload["capability_id"] != "source-navigation"
            or payload["target"] != target
            or payload["freshness_status"] != "current"
            or payload["domain_verdict"] != "pass"
            or payload["projection_status"] != "pass"
            or not isinstance(payload["result"], dict)
            or not isinstance(payload["result"]["_projection"], dict)):
        raise ValueError("navigation result is not current and passing")
    return payload


def _navigate_known_symbols(workspace: Path, compiler: Path, python: Path,
                            action: dict) -> tuple[dict, list[dict]]:
    targets = action.get("navigation_targets")
    if targets is None:
        return action, []
    code = "NOKIY_FULL_STACK_NAVIGATION_UNVERIFIED"
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 4
            or any(not isinstance(target, str) or not _NAVIGATION_TARGET.fullmatch(target)
                   for target in targets) or len(set(targets)) != len(targets)
            or not isinstance(action.get("context_summary"), str)
            or not action["context_summary"].strip() or action.get("task_projection") is not None):
        raise caller.EmbeddedNokiyError(code, "navigation requires 1..4 distinct exact symbols and a context summary")
    locators: list[dict] = []
    generation = None
    for target in targets:
        argv = [str(python), "-B", str(compiler), "query", "--capability", "source-navigation",
                "--target", target, "--depth", "1", "--json"]
        try:
            payload = _query_navigation(workspace, argv, target)
            current_generation = payload["generation_id"]
            if not isinstance(current_generation, str) or not current_generation:
                raise ValueError("navigation generation is missing")
            if generation is not None and current_generation != generation:
                raise ValueError("navigation generations differ")
            result = payload["result"]
            projection = result["_projection"]
            if projection["complete"] is True:
                resolved = result["resolved"]
            elif projection["complete"] is False:
                digest = projection["full_result_sha256"]
                if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,254}", current_generation)
                        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                    raise ValueError("navigation continuation binding is malformed")
                # Graph pagination need not block one exact locator. Select only
                # /resolved, bound to the original full result, once (no page loop).
                payload = _query_navigation(workspace, argv + [
                    "--result-pointer", "/resolved", "--expected-generation-id", current_generation,
                    "--expected-full-result-sha256", digest,
                ], target)
                result = payload["result"]
                projection = result["_projection"]
                if (payload["generation_id"] != current_generation
                        or result["result_pointer"] != "/resolved"
                        or type(result["item_count"]) is not int or result["item_count"] != 1
                        or projection["complete"] is not True
                        or projection["full_result_sha256"] != digest
                        or type(projection["page_offset"]) is not int or projection["page_offset"] != 0
                        or type(projection["returned_item_count"]) is not int
                        or projection["returned_item_count"] != 1
                        or projection["next_offset"] is not None
                        or any(projection.get(key, []) != []
                               for key in ("deferred_item_refs", "deferred_value_refs"))
                        or any(type(projection.get(key, 0)) is not int or projection.get(key, 0) != 0
                               for key in ("omitted_item_count", "deferred_item_count"))):
                    raise ValueError("navigation selection is not bound and complete")
                resolved = result["items"]
            else:
                raise ValueError("navigation completeness is malformed")
            if not isinstance(resolved, list) or len(resolved) != 1:
                raise ValueError("navigation result is not current and complete")
            symbol = resolved[0]
            path = symbol["payload"]["path"]
            line = symbol["payload"]["line"]
            if (symbol["entity_id"] != target or not isinstance(path, str)
                    or not _NAVIGATION_PATH.fullmatch(path)
                    or path.startswith("/") or ".." in Path(path).parts
                    or type(line) is not int or line < 1):
                raise ValueError("navigation did not resolve an exact source location")
            generation = current_generation
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise caller.EmbeddedNokiyError(code, "DCF navigation result is not an exact current locator") from error
        locators.append({"target": target, "path": path, "line": line,
                         "generation_id": generation})
    compiled_action = dict(action)
    compiled_action.pop("navigation_targets")
    lines = [f"{row['target']} -> {row['path']}:{row['line']}" for row in locators]
    compiled_action["context_summary"] = (
        action["context_summary"].strip()
        + "\nDCF source-navigation locators (context only; no read/write grant):\n"
        + "\n".join(lines)
    )
    return compiled_action, locators


def _auto_excerpt_eligible(draft: dict, contract: dict, locators: list) -> bool:
    """Optimize an exact read, never a discovery, verification or mutation task."""
    operations = contract.get("allowed_operations", [])
    return (
        draft.get("authority_effect") == "none"
        and draft.get("require_tool_call") is False
        and len(locators) == 1 and locators[0]["path"].endswith(".py")
        and contract.get("read_scopes") == [locators[0]["path"]]
        and contract.get("write_scopes") == []
        and isinstance(operations, list) and "read" in operations
        and all(operation in ("read", "command") for operation in operations)
        and ("command" not in operations or contract.get("source_read") is True)
        and "read" not in contract.get("denied_operations", [])
        and not contract.get("command_templates")
        and not any(contract.get(key) for key in
                    ("verifier_commands", "verifier_grants", "verifier_command_templates"))
    )


def _auto_edit_excerpt_eligible(draft: dict, contract: dict, locators: list) -> bool:
    """Select supported edit shapes without rejecting other valid task shapes."""
    operations = contract.get("allowed_operations", [])
    return (
        draft.get("authority_effect") == "workspace"
        and draft.get("require_tool_call") is True
        and 1 <= len(locators) <= 4
        and isinstance(operations, list)
        and "read" in operations and "modify" in operations
        and all(op in ("read", "modify", "command") for op in operations)
        and contract.get("source_read") is True
        and all(row["path"].endswith(".py")
                and row["path"] in contract.get("read_scopes", [])
                and row["path"] in contract.get("write_scopes", []) for row in locators)
    )


def prepare_request(draft_path: Path, action_path: Path, surface_id: str | None,
                    output: Path, *, topology_model: str | None = None,
                    worker_family: str | None = None) -> dict:
    """Compile once, validate through the real executor, and publish request last."""
    code = "NOKIY_FULL_STACK_PREPARATION_FAILED"
    draft = dict(caller._load_json(draft_path, limit=caller.MAX_REQUEST_BYTES, code=code))
    selection = None
    if topology_model is not None:
        from .model_topology import project_worker_draft
        draft, selection = project_worker_draft(draft, topology_model, worker_family)
    elif worker_family is not None:
        raise caller.EmbeddedNokiyError("NOKIY_TOPOLOGY_MODE_REQUIRED",
                                         "worker-family requires topology-model")
    action = caller._load_json(action_path, limit=caller.MAX_REQUEST_BYTES, code=code)
    if "focused_verifiers" in action:
        raise caller.EmbeddedNokiyError(
            code, "action.focused_verifiers is unsupported; use action.verifier_commands")
    if "terminal_delivery" in draft:
        raise caller.EmbeddedNokiyError(code, "terminal_delivery belongs in action, not draft")
    if "initial_task_state" in draft:
        raise caller.EmbeddedNokiyError(code, "initial_task_state belongs in action, not draft")
    initial_task_state = None
    if "initial_task_state" in action:
        initial_task_state = caller._normalize_initial_task_state(action.pop("initial_task_state"), code=code)
    terminal_delivery = action.pop("terminal_delivery", "assistant_reply")
    if (not isinstance(terminal_delivery, str)
            or terminal_delivery not in {"assistant_reply", "evidence_only"}):
        raise caller.EmbeddedNokiyError(code, "terminal_delivery must be assistant_reply or evidence_only")
    auto_source_excerpt = "include_source_excerpt" not in action
    include_source_excerpt = action.pop("include_source_excerpt", False)
    excerpt = None
    excerpt_decision = "ineligible" if auto_source_excerpt else "disabled"
    if type(include_source_excerpt) is not bool:
        raise caller.EmbeddedNokiyError(code, "include_source_excerpt must be boolean")
    if surface_id is not None and (not isinstance(surface_id, str) or not surface_id.strip()):
        raise caller.EmbeddedNokiyError(code, "surface_id must be an exact nonempty ID")
    if {"context_capsule", "jspace_contract", "request_id", "request_sha256", "model_selection"} & draft.keys():
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
    draft.setdefault("timeout_seconds", None)
    draft.setdefault("max_trajectory_bytes", None)
    draft.setdefault("native_thread_id", os.environ.get("CODEX_THREAD_ID"))
    draft.setdefault("persistence_mode", "native_codex_thread_only")
    if (draft["schema_version"] != caller.REQUEST_SCHEMA_VERSION
            or draft["execution_profile"] not in {"direct", "balanced"}
            or not draft["native_thread_id"]
            or draft["native_thread_id"] != os.environ.get("CODEX_THREAD_ID")):
        raise caller.EmbeddedNokiyError(code, "current native thread and full-core profile required")
    if terminal_delivery == "evidence_only":
        draft["terminal_delivery"] = terminal_delivery
    if initial_task_state is not None:
        draft["initial_task_state"] = initial_task_state
    from .local_context import MODE, compile_context, dcf_root
    managed_root = dcf_root(workspace)
    if managed_root is None:
        if "surface_targets" in action:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_TARGET_INVALID",
                                             "surface_targets require a DCF workspace")
        if include_source_excerpt:
            raise caller.EmbeddedNokiyError(code, "source excerpt requires a DCF workspace")
        if "navigation_targets" in action:
            raise caller.EmbeddedNokiyError(code, "source-navigation is available only in a DCF workspace")
        if surface_id is not None:
            raise caller.EmbeddedNokiyError(code, "DCF surface requested but workspace has no DCF; omit surface for local context")
        artifact_root = (caller._plain_path(draft.get("artifact_root"), name="artifact_root", directory=True)
                         if "verifier_commands" in action else None)
        capsule, contract = compile_context(workspace, action, artifact_root=artifact_root)
        compiler_before = _identity(Path(__file__).with_name("local_context.py"))
        context_mode = MODE
    else:
        if managed_root != workspace or not compiler.is_file() or not python.is_file():
            raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_DCF_UNAVAILABLE", "use the DCF root and its working interpreter; no local downgrade")
        if "verifier_commands" in action:
            artifact_root = caller._plain_path(draft.get("artifact_root"), name="artifact_root", directory=True)
            if action.get("verifier_artifact_root", str(artifact_root)) != str(artifact_root):
                raise caller.EmbeddedNokiyError(
                    "NOKIY_FULL_STACK_VERIFIER_ARTIFACT_ROOT_MISMATCH",
                    "verifier artifact root differs from request",
                )
            action = dict(action, verifier_artifact_root=str(artifact_root))
        resolution = None
        target_identities = {}
        if "surface_targets" in action:
            action, surface_id, resolution, target_identities = _resolve_surface_targets(
                workspace, python, action, surface_id)
        if not isinstance(surface_id, str) or not surface_id.strip():
            raise caller.EmbeddedNokiyError(code, "DCF-managed workspace requires an exact surface")
        if "navigation_targets" in action:
            capsule, contract, compiler_before, action, locators = _compile_navigated_dcf(
                workspace, compiler, python, surface_id, action)
        else:
            locators = []
            capsule, contract, compiler_before = _compile_dcf(workspace, compiler, python, surface_id, action)
        if resolution is not None:
            if (contract.get("dcf_generation", {}).get("generation_id") != resolution["generation_id"]
                    or contract.get("matched_surface_ids") != [surface_id]
                    or not _surface_files_still_current(workspace, target_identities)):
                raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED,
                                                 "surface resolution drifted before J-Space compilation")
        if (locators and contract.get("dcf_generation", {}).get("generation_id")
                != locators[0]["generation_id"]):
            raise caller.EmbeddedNokiyError(
                "NOKIY_FULL_STACK_NAVIGATION_UNVERIFIED",
                "DCF generation changed between navigation and J-Space compilation",
            )
        auto_excerpt = auto_source_excerpt and _auto_excerpt_eligible(draft, contract, locators)
        # Explicit true retains its original single-symbol readonly semantics;
        # Only new automatic edit preparation opts into envelope-bounded allocation.
        edit_excerpt = auto_source_excerpt and _auto_edit_excerpt_eligible(draft, contract, locators)
        if include_source_excerpt or auto_excerpt or edit_excerpt:
            if not edit_excerpt and (draft.get("authority_effect") != "none"
                    or draft.get("require_tool_call") is not False or len(locators) != 1):
                raise caller.EmbeddedNokiyError(
                    "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "one read-only, no-tool DCF symbol required",
                )
            from .source_excerpt import (
                CONTEXT_MARKER, MAX_ENVELOPE_BYTES, ExcerptBudgetExceeded,
                extract, extract_edits, still_current,
            )
            if auto_excerpt or edit_excerpt:
                try:
                    excerpt = (extract_edits(workspace, contract, locators, per_target_budget=True,
                                             support_navigation=True, envelope_bounded=True)
                               if edit_excerpt else
                               extract(workspace, contract, locators[0], include_dependencies=True))
                    if len(json.dumps(excerpt, ensure_ascii=True).encode()) > MAX_ENVELOPE_BYTES:
                        raise ExcerptBudgetExceeded(
                            "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "serialized excerpt exceeds context budget",
                        )
                except ExcerptBudgetExceeded:
                    # No partial/complete claim: leave navigation and all tools
                    # intact. Large edit targets require ordinary source_read.
                    excerpt = None
                    excerpt_decision = "capacity_exceeded"
            else:
                excerpt = extract(workspace, contract, locators[0])
            if excerpt is not None:
                excerpt_decision = "auto_edit" if edit_excerpt else "auto" if auto_excerpt else "explicit"
        if excerpt is not None:
            augmented = dict(action)
            augmented["context_summary"] = (
                action["context_summary"].strip()
                + CONTEXT_MARKER
                + json.dumps(excerpt, sort_keys=True, ensure_ascii=True)
            )
            if not still_current(workspace, excerpt):
                raise caller.EmbeddedNokiyError(
                    "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "source changed before capsule rebinding",
                )
            capsule = _rebind_dcf_capsule(workspace, compiler, python, compiler_before,
                                          contract, augmented)
            if (contract.get("dcf_generation", {}).get("generation_id")
                    != locators[0]["generation_id"]
                    or not still_current(workspace, excerpt)
                    or _identity(compiler) != compiler_before):
                raise caller.EmbeddedNokiyError(
                    "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "source or DCF compiler changed",
                )
            action = augmented
        if resolution is not None and (contract.get("dcf_generation", {}).get("generation_id")
                                       != resolution["generation_id"]
                                       or not _surface_files_still_current(workspace, target_identities)):
            raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED, "surface resolution drifted during preparation")
        context_mode = "dcf_jspace_required"
    if "verifier_commands" in action and (
            contract.get("verifier_commands") != action["verifier_commands"]
            or contract.get("verifier_artifact_root") != draft.get("artifact_root")):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_VERIFIER_UNBOUND",
                                         "compiler did not bind the requested exact verifier grants")
    # A used directory is never reused, even after a failed prepare. Do not erase evidence.
    output.mkdir(mode=0o700)
    caller._write_create_only(output / "capsule.json", capsule)
    caller._write_create_only(output / "jspace.json", contract)
    draft.update(context_capsule=_identity(output / "capsule.json"),
                 jspace_contract=_identity(output / "jspace.json"))
    if selection is not None:
        from .model_topology import request_marker
        draft["model_selection"] = request_marker(selection)
    request = caller.decode_request(draft)
    ready = caller.preflight(request)
    if context_mode == "dcf_jspace_required" and resolution is not None:
        if not _surface_files_still_current(workspace, target_identities):
            raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED,
                                             "surface targets changed during preflight")
    preparation = {
        "status": "PREPARED", "execution_profile": request.execution_profile,
        "context_mode": context_mode, "request_id": request.request_id,
        "compiler": compiler_before, "action": action, "preflight": ready,
        "provider_execution_started": False,
    }
    if context_mode == "dcf_jspace_required":
        preparation["navigation_locators"] = locators
        if resolution is not None:
            preparation["surface_resolution"] = resolution
        preparation["source_excerpt_decision"] = excerpt_decision
        if excerpt is not None:
            if excerpt.get("kind") == "edit_preimage":
                # Source bytes occur only in the capsule/action context, never
                # again in the public preparation receipt or per-symbol rows.
                preparation["source_excerpt"] = {
                    "kind": excerpt["kind"],
                    "files": [{"path": row["path"], "source_sha256": row["source_sha256"],
                               "spans": [{key: span[key] for key in ("start_line", "end_line")}
                                         for span in row["spans"]]} for row in excerpt["files"]],
                }
            else:
                preparation["source_excerpt"] = {
                    key: excerpt[key] for key in ("path", "start_line", "end_line", "source_sha256")
                }
    if selection is not None:
        from .model_topology import bind_prepared_request
        binding = bind_prepared_request(selection, request)
        caller._write_create_only(output / "model-selection.json", binding)
        preparation["model_selection"] = _identity(output / "model-selection.json")
        preparation["root_topology_verified"] = False
    caller._write_create_only(output / "preparation.json", preparation)
    caller._write_create_only(output / "request.json", request.to_wire(include_identity=False))
    public_preparation = {key: value for key, value in preparation.items() if key != "action"}
    action_bytes = caller._canonical_bytes(action)
    public_action = {"sha256": hashlib.sha256(action_bytes).hexdigest(), "bytes": len(action_bytes)}
    summary = action.get("context_summary")
    if isinstance(summary, str):
        public_action["context_summary_sha256"] = hashlib.sha256(summary.encode()).hexdigest()
        public_action["context_summary_chars"] = len(summary)
    public_preparation["action"] = public_action
    return {**public_preparation, "request": _identity(output / "request.json")}


def _compile_navigated_dcf(workspace, compiler, python, surface_id, action):
    from .dcf_prepare import bind_locators, targets_for
    code = "NOKIY_FULL_STACK_NAVIGATION_UNVERIFIED"
    bridge = Path(__file__).with_name("dcf_prepare.py")
    compiler_before, bridge_before = _identity(compiler), _identity(bridge)
    try:
        targets_for(action)
        completed = subprocess.run(
            [str(python), "-B", str(bridge)], cwd=workspace,
            input=json.dumps({"surface_id": surface_id, "action": action}),
            capture_output=True, text=True, timeout=60,
        )
        if completed.returncode or len(completed.stdout.encode()) > caller.MAX_REQUEST_BYTES:
            raise ValueError("DCF preparation failed or response exceeded budget")
        compiled = json.loads(completed.stdout)
        capsule, contract = compiled["task_context_capsule"], compiled["contract"]
        if not isinstance(capsule, dict) or not isinstance(contract, dict):
            raise ValueError("DCF did not provide canonical capsule/contract")
        locators = compiled["navigation_locators"]
        amended = bind_locators(action, locators, contract["dcf_generation"]["generation_id"])
        if (compiled["action"] != amended
                or capsule.get("context_summary") != amended["context_summary"]
                or capsule.get("dcf_generation") != contract["dcf_generation"]
                or _identity(compiler) != compiler_before or _identity(bridge) != bridge_before):
            raise ValueError("DCF context, generation or implementation changed")
        return capsule, contract, compiler_before, amended, locators
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as error:
        raise caller.EmbeddedNokiyError(code, "single-pass DCF preparation did not verify") from error


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


def _rebind_dcf_capsule(workspace: Path, compiler: Path, python: Path,
                        compiler_before: dict, contract: dict, action: dict) -> dict:
    """Bind context through DCF's canonical compiler; never recompile authority."""
    code = "NOKIY_SOURCE_EXCERPT_UNVERIFIED"
    if _identity(compiler) != compiler_before:
        raise caller.EmbeddedNokiyError(code, "DCF compiler changed before rebinding")
    script = (
        "import json,sys;"
        "from scripts.ops.dcf.jspace import (canonical_contract_bytes,"
        "compile_task_context_capsule,render_task_context_capsule);"
        "data=json.load(sys.stdin);contract=data['contract'];"
        "canonical_contract_bytes(contract);"
        "capsule=compile_task_context_capsule(contract,action=data['action']);"
        "render_task_context_capsule(capsule,expected_task_id=data['action']['mission']['task_id'],"
        "expected_jspace_sha256=contract['authorization_semantic_sha256']);"
        "json.dump(capsule,sys.stdout,sort_keys=True)"
    )
    try:
        completed = subprocess.run(
            [str(python), "-B", "-c", script],
            input=json.dumps({"contract": contract, "action": action}), cwd=workspace,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError(code, "DCF capsule compiler did not finish") from error
    if completed.returncode or len(completed.stdout.encode()) > caller.MAX_REQUEST_BYTES:
        raise caller.EmbeddedNokiyError(code, "DCF capsule rebinding failed or exceeded budget")
    try:
        capsule = json.loads(completed.stdout)
        if (not isinstance(capsule, dict)
                or capsule.get("jspace_semantic_sha256")
                != contract["authorization_semantic_sha256"]
                or capsule.get("dcf_generation") != contract["dcf_generation"]
                or capsule.get("mission", {}).get("task_id") != action["mission"]["task_id"]
                or capsule.get("context_summary") != action["context_summary"].strip()
                or capsule.get("semantic_sha256") != caller._canonical_sha256(
                    {key: value for key, value in capsule.items() if key != "semantic_sha256"})):
            raise ValueError("capsule does not bind the original contract and action")
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise caller.EmbeddedNokiyError(code, "DCF capsule rebinding was not canonical") from error
    if _identity(compiler) != compiler_before:
        raise caller.EmbeddedNokiyError(code, "DCF compiler changed during rebinding")
    return capsule
