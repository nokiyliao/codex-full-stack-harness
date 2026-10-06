# SPDX-License-Identifier: MIT
"""nokiy: a Codex-owned execution core."""
from __future__ import annotations

import argparse
import codecs
from collections import deque
from collections.abc import Iterable
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any

from . import embedded_nokiy as caller
from . import file_change_evidence
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
MAX_COMMAND_EVIDENCE = 128
MAX_ENGINE_SUMMARY_BYTES = 1024 * 1024
MAX_SUPERVISION_BYTES = 2 * 1024 * 1024
RECEIPT_ROOT_ENV = "TURA_COMMAND_RECEIPT_ROOT"
RECEIPT_WORKSPACE_ENV = "TURA_COMMAND_RECEIPT_WORKSPACE"
RECEIPT_CAPABILITY = b"nokiy_command_receipt_binding_v1"
BUDGET_ENV = "NOKIY_EXECUTION_BUDGET"
BUDGET_SCHEMA = "nokiy_execution_budget_v1"
INITIAL_TASK_STATE_ENV = "TURA_NOKIY_INITIAL_TASK_STATE"
INITIAL_TASK_STATE_SCHEMA = "nokiy_initial_task_state_v1"
MAX_INITIAL_TASK_STATE_BYTES = 4096
PHASE_TIMING_SCHEMA = "nokiy_full_core_phase_timing_v1"
MAX_PHASE_TIMING_BYTES = 4096
MAX_KNOWN_SOURCE_SHA256_ENTRIES = 32
MAX_KNOWN_SOURCE_SHA256_BYTES = 4096
MAX_DIRECTORY_READ_PRESENTATION_BYTES = 4096
CAPABILITY_GAP_SCHEMA = "nokiy_capability_gap_v1"
CAPABILITY_GAP_GUIDANCE = (
    "Capability-gap handoff (model-reported diagnostic, not a grant): If a missing "
    "path/operation/tool blocks unfinished worker-assigned work, preserve the first actual blocker. "
    "Explicitly parent-owned verification/deployment/acceptance are not worker blockers and must "
    "not be claimed passed. Missing verifier capability alone never transfers required worker "
    "verification to the parent or excuses it. Do not self-grant, switch denied paths or replay "
    "effects. Return exactly one JSON "
    "fence opened by ```nokiy_capability_gap_v1 on its own line and closed by ``` on "
    "its own line. Use only keys schema_version (nokiy_capability_gap_v1), "
    "missing_capabilities (1..8 distinct objects with only path, operation, tool), "
    "completed_work (0..8 nonempty strings), remaining_work (1..8 nonempty strings), "
    "unified_diff (string or null). Each work string is at most 512 UTF-8 bytes. "
    "operation is read/modify/create/delete/command; tool is an exact identifier, "
    "not argv. path is an exact workspace-relative path (at most 256 UTF-8 bytes), "
    "never absolute, wildcard, .git, traversal or symlink; null is only for a pathless "
    "command/tool gap. Keep JSON at most 8192 UTF-8 bytes. Supply a usable plain "
    "unified diff (--- a/path, +++ b/path, @@ hunks; /dev/null for create/delete; "
    "no git metadata, at most 4096 UTF-8 bytes) only when current admitted reads "
    "suffice, and list every proposed target's missing modify/create/delete capability. "
    "Otherwise use null and name the exact missing read evidence in missing_capabilities. "
    "A missing mutation capability requires a diff or a precise missing read. Never substitute "
    "a placeholder patch or claim an absent patch/tests executed. "
    "Publish the entire valid gap fence with a usable unified diff or precise missing-read "
    "evidence as visible assistant text BEFORE any terminal task_status question/done call. "
    "These statuses can end the run when a visible proposal is already present. "
    "Never promise a patch below or in a later turn. Completed work "
    "is model-reported, not execution proof, safe replay or mission success. "
    "The parent must recover effects/cleanup and freshly re-admit only authorized "
    "unfinished work; this handoff changes no permissions."
)
_PHASE_CHECKPOINTS = ("engine_started", "prepare", "session_db_ready", "router_ready",
                      "cli_launch", "cli_process", "trajectory_parse", "cleanup_started", "cleanup")


def _scrub_receipt_binding(env: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items()
            if key not in {RECEIPT_ROOT_ENV, RECEIPT_WORKSPACE_ENV}}


def _exact_presentation_path(value: Any, *, absolute: bool) -> str | None:
    """Accept literal paths only; never resolve links or normalize aliases."""
    if (not isinstance(value, str) or not value
            or len(value) > MAX_KNOWN_SOURCE_SHA256_BYTES
            or value.startswith("/") != absolute
            or any(char in value for char in "\\*?[]{}~")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return None
    parts = (value[1:] if absolute else value).split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return value


def _known_source_sha256_by_path(jspace: dict[str, Any],
                                 verifiers: list[dict[str, Any]]) -> dict[str, str]:
    """Present only immutable exact caller pins, not newly discovered evidence."""
    operations = jspace.get("allowed_operations")
    denied = jspace.get("denied_operations", [])
    if (jspace.get("source_read") is not True or not isinstance(operations, list)
            or not all(isinstance(operation, str) for operation in operations)
            or "command" not in operations or "read" not in operations
            or not isinstance(denied, list)
            or not all(isinstance(operation, str) for operation in denied)
            or "command" in denied or "read" in denied):
        return {}
    root = jspace.get("repo_root")
    if root != "/" and _exact_presentation_path(root, absolute=True) is None:
        return {}
    prefix = root if root == "/" else root + "/"
    read_scopes = jspace.get("read_scopes")
    write_scopes = jspace.get("write_scopes")
    if not isinstance(read_scopes, list) or not isinstance(write_scopes, list):
        return {}
    reads = {scope for scope in read_scopes
             if _exact_presentation_path(scope, absolute=False) is not None}
    writes = set()
    for scope in write_scopes:
        absolute = isinstance(scope, str) and scope.startswith("/")
        if _exact_presentation_path(scope, absolute=absolute) is None:
            return {}
        if absolute:
            if not scope.startswith(prefix):
                return {}
            scope = scope[len(prefix):]
        writes.add(scope)
    bindings: dict[str, str | None] = {}
    for command in verifiers:
        pins = command.get("pinned_files", [])
        if not isinstance(pins, list):
            return {}
        for pin in pins:
            if not isinstance(pin, dict):
                continue
            path = pin.get("path")
            sha256 = pin.get("sha256")
            if (_exact_presentation_path(path, absolute=True) is None
                    or not path.startswith(prefix)):
                continue
            path = path[len(prefix):]
            if (path not in reads or any(path == scope or path.startswith(scope + "/")
                                        or scope.startswith(path + "/") for scope in writes)):
                continue
            digest = (sha256.lower() if isinstance(sha256, str) and len(sha256) == 64
                      and all(char in "0123456789abcdefABCDEF" for char in sha256) else None)
            if path in bindings and bindings[path] != digest:
                bindings[path] = None
            else:
                bindings[path] = digest
    presented = {}
    size = 2  # Compact ASCII JSON braces; omission never changes read authority.
    for path, digest in sorted(bindings.items()):
        if digest is None:
            continue
        if len(presented) >= MAX_KNOWN_SOURCE_SHA256_ENTRIES:
            break
        pair_size = len(json.dumps(path, ensure_ascii=True)) + len(digest) + 3
        pair_size += bool(presented)  # Colon, hash quotes, and any separating comma.
        if size + pair_size > MAX_KNOWN_SOURCE_SHA256_BYTES:
            continue
        presented[path] = digest
        size += pair_size
    return presented


def _directory_read_presentation(jspace: dict[str, Any]) -> dict[str, Any] | None:
    """Project bounded existing directory commands; never probe or grant paths."""
    policy = jspace.get("read_commands")
    if not isinstance(policy, dict) or set(policy) != {"roots", "rg", "cat"}:
        return None
    operations = jspace.get("allowed_operations")
    denied = jspace.get("denied_operations", [])
    scopes = jspace.get("read_scopes")
    if (any(not isinstance(items, list) or len(items) > 64
            or not all(isinstance(item, str) and item for item in items)
            for items in (operations, denied, scopes))
            or "command" not in operations or "read" not in operations
            or "command" in denied or "read" in denied):
        return None
    roots = policy["roots"]
    if not isinstance(roots, list) or not 1 <= len(roots) <= 8:
        return None
    for root in roots:
        if (_exact_presentation_path(root, absolute=False) is None
                or not root.strip() or root == "-"
                or any(part.startswith(".") for part in root.split("/"))
                or root + "/**" not in scopes):
            return None
        try:
            if len(root.encode("utf-8")) > MAX_DIRECTORY_READ_PRESENTATION_BYTES:
                return None
        except UnicodeEncodeError:
            return None
    if len(set(roots)) != len(roots):
        return None
    presented = {"roots": list(roots)}
    for name in ("rg", "cat"):
        pin = policy[name]
        if not isinstance(pin, dict) or set(pin) != {"path", "sha256"}:
            return None
        path, digest = pin["path"], pin["sha256"]
        if (_exact_presentation_path(path, absolute=True) is None
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            return None
        try:
            if len(path.encode("utf-8")) > MAX_DIRECTORY_READ_PRESENTATION_BYTES:
                return None
        except UnicodeEncodeError:
            return None
        presented[name] = {"path": path, "sha256": digest}
    presented["listing_example"] = {
        "commands": [{"command_type": "bash", "step": 1,
                      "command_line": shlex.join([presented["rg"]["path"], "--no-config",
                                                  "--max-filesize=1M", "--files", "--", roots[0]])}],
    }
    presented["cat_argv_prefix"] = [presented["cat"]["path"], "--"]
    if len(json.dumps(presented, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
           ) > MAX_DIRECTORY_READ_PRESENTATION_BYTES:
        return None
    return presented


def _capability_gap_prompt(prompt: str) -> str:
    return prompt + ("" if prompt.endswith("\n") else "\n") + CAPABILITY_GAP_GUIDANCE


_EVIDENCE_ONLY_COMPLETION_GUIDANCE = (
    "\n\nEvidence-only completion: No prose-only recap. Complete worker work and required checks/"
    "readbacks before existing task_status done. Explicitly parent-owned checks are not worker blockers; "
    "never claim them passed. Parent acceptance remains required. "
    "When task instructions permit, semantic decisions are settled, all remaining exact artifacts/"
    "effects/checks/readbacks are known and need no result-dependent interpretation, allow final "
    "command_run: planned apply_patch step 1, deterministic verification/readbacks step 2, task_status "
    "done step 3 (strictly later positive steps, same response). Checks must pass before done executes, "
    "not before proposing. Failure/timeout/uncertainty or result-dependent review needs a later model "
    "repair/review turn. No done on failed/unknown effects, skipped required checks, or unfinished work. "
    "Missing verifiers never transfer required worker verification to the parent or excuse it. For real "
    "worker blockers, publish the visible capability-gap handoff before terminal task_status question/"
    "done; do not self-grant tools. Scope/effect/receipt gates still apply.\n"
)


def _provider_prompt(prompt: str, jspace: dict[str, Any],
                     terminal_delivery: str = "assistant_reply") -> str:
    """Present verified tool capabilities, without changing their enforcement."""
    if terminal_delivery == "evidence_only":
        prompt += _EVIDENCE_ONLY_COMPLETION_GUIDANCE
    operations = jspace.get("allowed_operations", [])
    command_allowed = isinstance(operations, list) and "command" in operations
    read_allowed = (command_allowed and jspace.get("source_read") is True
                    and "read" in operations)
    verifiers = jspace.get("verifier_commands")
    verifier_allowed = (command_allowed and isinstance(verifiers, list) and bool(verifiers)
                        and all(isinstance(command, dict)
                                and isinstance(command.get("argv"), list) and command["argv"]
                                and all(isinstance(arg, str) and arg for arg in command["argv"])
                                for command in verifiers))
    directory_routes = _directory_read_presentation(jspace)
    if not (read_allowed or verifier_allowed or directory_routes):
        return _capability_gap_prompt(prompt)
    projection = {
        "jspace_semantic_sha256": jspace.get("authorization_semantic_sha256",
                                             jspace.get("semantic_sha256")),
        "allowed_operations": operations,
        "denied_operations": jspace.get("denied_operations", []),
    }
    if directory_routes:
        projection["read_commands"] = directory_routes
    if read_allowed:
        projection.update(schema_version="nokiy_source_read_presentation_v1",
                          source_read=True, read_scopes=jspace.get("read_scopes", []))
        if verifier_allowed:
            known = _known_source_sha256_by_path(jspace, verifiers)
            if known:
                projection["known_source_sha256_by_path"] = known
        scopes = projection["read_scopes"]
        denied = projection["denied_operations"]
        if (isinstance(scopes, list) and isinstance(denied, list)
                and "read" not in denied and "command" not in denied):
            for scope in scopes:
                path = _exact_presentation_path(scope, absolute=False)
                if path is None:
                    continue
                try:
                    if len(path.encode("utf-8")) > 256:
                        continue
                except UnicodeEncodeError:
                    continue
                arguments = {"path": path, "start_line": 1, "end_line": 80}
                known = projection.get("known_source_sha256_by_path", {})
                if path in known:
                    arguments["expected_sha256"] = known[path]
                projection["source_read_example"] = {
                    "commands": [{"command_type": "source_read",
                                  "command_line": json.dumps(arguments, sort_keys=True,
                                                             separators=(",", ":"),
                                                             ensure_ascii=True),
                                  "step": 1}],
                }
                break
    if verifier_allowed:
        projection["focused_verifier"] = {
            "verifier_indices": list(range(len(verifiers))),
            "example": {"commands": [{"command_type": "focused_verifier",
                                      "command_line": '{"verifier_index":0}', "step": 1}]},
        }
    write_scopes = jspace.get("write_scopes")
    read_scopes = projection.get("read_scopes")
    denied = projection["denied_operations"]
    directory_read_scopes = ({root + "/**" for root in directory_routes["roots"]}
                             if directory_routes else set())
    read_write_allowed = (read_allowed and "modify" in operations
        and all(isinstance(op, str) and op for op in operations)
        and isinstance(denied, list) and all(isinstance(op, str) and op for op in denied)
        and not any(op in denied for op in ("command", "read", "modify"))
        and isinstance(write_scopes, list) and bool(write_scopes)
        and isinstance(read_scopes, list) and bool(read_scopes)
        and all(_exact_presentation_path(scope, absolute=False) is not None
                for scope in write_scopes)
        and all(_exact_presentation_path(scope, absolute=False) is not None
                or (isinstance(scope, str) and scope in directory_read_scopes)
                for scope in read_scopes)
        and any(scope in read_scopes for scope in write_scopes))
    if read_write_allowed:
        try:
            for scope in write_scopes + read_scopes:
                scope.encode("utf-8")
        except UnicodeEncodeError:
            read_write_allowed = False
    text = (prompt + "\n\nVerified execution capability (caller projection):\n"
            + json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    if read_allowed:
        if verifier_allowed:
            text += ("\nCapsule: evidence, not a read grant. Empty declared_targets or "
                     "evidence_refs do not revoke scopes. "
                     "source_read: bounded exact scoped JSON path/lines/search_terms. "
                     "Batch independent reads; no speculative dependencies/whole-file dumps. "
                     "expected_sha256: this path's current verbatim 64-hex SHA; omit iff unknown. "
                     "Never borrow another file's SHA. "
                     "Page: returned SHA/next_line. After mutation: fresh scoped readback, "
                     "not preimage SHA. runtime rechecks every read; no new grants.\n")
        else:
            text += ("\nCapsule: evidence, not a read grant. Empty declared_targets or "
                "evidence_refs do not revoke scopes. command_run: command_type source_read; "
                "JSON command_line: exact scoped path, bounded start_line/end_line or search_terms. "
                "Batch known independent reads in one command_run array, each scoped, "
                "with source hashes/pagination cursors; no speculative dependent reads or whole-file dumps. "
                "First read: omit expected_sha256 only if no current exact-path SHA is known. "
                "Never borrow another file's SHA; keep task-bound digests. "
                "SHA-256 must be exactly 64 hexadecimal characters copied verbatim, "
                "not shortened or reconstructed. "
                "Page same file: expected_sha256=returned SHA, start_line=next_line. "
                "After mutation: fresh scoped readback, not preimage SHA. "
                "Reuse verified unchanged ranges; reread for missing/changed evidence "
                "or explicit task requirements. Respect tool limits/pagination. "
                "Missing DCF locators do not block exact granted paths. "
                "runtime rechecks every read; no added operation/shell/path/write.\n")
        if "known_source_sha256_by_path" in projection:
            text += ("Use known_source_sha256_by_path only for the matching path's "
                     "pre-edit expected_sha256.\n")
    if directory_routes:
        text += ("\nread_commands: bash listing_example; shell-quote pinned cat_argv_prefix "
                 "plus a discovered path. source_read requires source_read:true and an exact "
                 "read_scopes path; directory scopes never qualify. No guessed paths, hidden "
                 "files, symlinks, traversal, pipelines, substitution, --pre or --follow; "
                 "runtime pins/limits apply. No new read/write authority.\n")
    if verifier_allowed:
        text += ("\nadmitted verifier_index only; "
                 "caller-bound, no new authority. Verifier-only step group; "
                 "whole-array task limits prevail. No task rewrites/invented capabilities. "
                 "Reuse caller-verified current pre-edit baseline; reproduce if required; "
                 "run required focused post-edit verification. "
                 "Verifier success + semantically reviewed fresh scoped postimage readback "
                 "covers only the exact unchanged state: no redundant tests/reads/status polls. "
                 "Reverify after relevant source/test/config changes, failure, "
                 "unknown/mismatched evidence or explicit task requirements. "
                 "Reuse evidence, never ownership/permission clearance. "
                 "Mandatory status updates and terminal fences. "
                 "No argv/cwd/timeout overrides or blocked/cancel bypass.\n")
        if read_write_allowed:
            text += ("Optional source_postimages: known exit0/reaped/group-empty success only. "
                     "Semantically review complete definitions/bindings + support, or all-text delta "
                     "on definition-size overflow (not complete definitions). "
                     "No dependency/file proof; exact paths/postimage SHA/lines only. "
                     "Missing/overflow/stale/uncovered evidence: ordinary granted source_read. "
                     "Preserve raw stdout/stderr, verifier/receipt/cleanup facts; no new authority, "
                     "acceptance or automatic done.\n")
        if ("modify" in operations and isinstance(denied, list)
                and "modify" not in denied and "command" not in denied
                and isinstance(write_scopes, list) and bool(write_scopes)
                and all(isinstance(scope, str) and scope for scope in write_scopes)):
            text += ("Task-permitted mixed responses: Once final patches are fully known, "
                     "exact-write patches precede the admitted focused_verifier "
                     "at a strictly later positive step in the same command_run response. "
                     "No dependent same-step verification or missing-evidence edits.\n")
    if read_write_allowed:
        if verifier_allowed:
            text += ("If post-edit evidence is present, review: "
                     "Mechanical missing readbacks only; mandatory task_status done "
                     "at a strictly later positive step in the same existing command_run. "
                     "Else propose fully-known mechanical checks/readbacks "
                     "in strict later steps per guidance. "
                     "No output-dependent decisions; safe exact read+write paths only. "
                     "Result-dependent review: later model round. Keep publication fences; "
                     "no failed/unknown-effect closure or expanded grants.\n")
        else:
            text += ("Source-read-only final edits: when task instructions permit a mixed response, "
                     "put only fully-known admitted final patches in earlier positive steps and "
                     "fully-known bounded fresh postimage source_read requests at a strictly later "
                     "positive step in the same command_run response. Use only safe exact write "
                     "paths also granted for read. Never use preimage SHA after mutation, "
                     "same-step dependent reads, speculative ranges or scope expansion. "
                     "Do not skip required verification, readback, status updates or terminal fences.\n")
    return _capability_gap_prompt(text)


def _verifier_fd() -> tuple[int, ...]:
    value = os.environ.get("NOKIY_VERIFIER_FD")
    if value is None:
        return ()
    fd = int(value)
    if fd < 3:
        raise ValueError("NOKIY_VERIFIER_FD_INVALID")
    return (fd,)


def prepare(request: caller.EmbeddedNokiyRequest):
    caller._verify_native_thread_binding(request)
    if request.schema_version != caller.REQUEST_SCHEMA_VERSION or request.execution_profile not in {"direct", "balanced"}:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_PROFILE_INVALID", "explicit current request required")
    if request.model_provider is not None:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_PROVIDER_OVERRIDE_UNSUPPORTED", "provider is bound by the image")
    run_root = request.artifact_root / request.request_id
    if run_root.is_relative_to(request.workspace) or request.workspace.is_relative_to(run_root):
        raise caller.EmbeddedNokiyError(
            "NOKIY_FULL_CORE_ARTIFACT_WORKSPACE_OVERLAP",
            "Full-core state root must be disjoint from workspace; choose an existing "
            "task-owned sibling artifact directory outside workspace",
        )
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
    capability_env = _scrub_receipt_binding(os.environ)
    capability_env.pop("NOKIY_VERIFIER_FD", None)
    try:
        capability = subprocess.run([str(runtime.artifacts["tura_router"].path),
            "command-receipt-capabilities"], capture_output=True, timeout=10, env=capability_env)
        supported = capability.returncode == 0 and capability.stdout == RECEIPT_CAPABILITY + b"\n"
    except (OSError, subprocess.TimeoutExpired):
        supported = False
    if not supported:
        ready.update(status="BLOCKED", first_typed_blocker="NOKIY_FULL_CORE_COMMAND_RECEIPT_BINDING_REQUIRED")
    if ready["status"] == "READY":
        runtime_bytes = runtime.artifacts["tura_runtime"].path.read_bytes()
        if BUDGET_SCHEMA.encode() not in runtime_bytes:
            ready.update(status="BLOCKED", first_typed_blocker="NOKIY_FULL_CORE_EXECUTION_BUDGET_REQUIRED")
        elif (request.initial_task_state is not None
              and INITIAL_TASK_STATE_SCHEMA.encode() not in runtime_bytes):
            ready.update(status="BLOCKED", first_typed_blocker="NOKIY_FULL_CORE_INITIAL_TASK_STATE_REQUIRED")
    if jspace.get("verifier_commands"):
        try:
            capability = subprocess.run([str(runtime.artifacts["tura_router"].path),
                "focused-verifier-capabilities"], capture_output=True, timeout=10,
                env=capability_env)
            supported = not capability.returncode and capability.stdout.strip() == b"nokiy_focused_verifier_parent_v1"
        except (OSError, subprocess.TimeoutExpired):
            supported = False
        if not supported and ready["status"] == "READY":
            ready.update(status="BLOCKED", first_typed_blocker="NOKIY_FULL_CORE_VERIFIER_PARENT_CHANNEL_REQUIRED")
    return ready, runtime, capsule, jspace


def _supervisor_environment(request) -> dict[str, str]:
    env = dict(os.environ)
    env.pop(BUDGET_ENV, None)
    if request.timeout_seconds is None:
        return env
    timeout_ms = request.timeout_seconds * 1000
    # Mint once at the hard supervisor boundary, never from an inherited hint.
    env[BUDGET_ENV] = json.dumps({"schema_version": BUDGET_SCHEMA,
        "request_sha256": request.request_sha256, "timeout_ms": timeout_ms,
        "deadline_unix_ms": time.time_ns() // 1_000_000 + timeout_ms,
        "reserve_ms": min(timeout_ms // 10, 10_000)}, sort_keys=True, separators=(",", ":"))
    return env


def _environment(request, runtime, state: Path, *, execution_budget: str | None = None) -> dict[str, str]:
    env = _scrub_receipt_binding(os.environ)
    for key in tuple(env):
        if key.startswith("TURA_") or key in {"SESSION_LOG_DB_ROOT", "LOG_PATH", "NOKIY_VERIFIER_FD", BUDGET_ENV}:
            env.pop(key)
    if request.timeout_seconds is not None and execution_budget is not None:
        env[BUDGET_ENV] = execution_budget
    env.update({
        "TURA_HOME": str(state), "TURA_DB_ROOT": str(state),
        RECEIPT_ROOT_ENV: str(state), RECEIPT_WORKSPACE_ENV: str(request.workspace),
        "TURA_PROJECT_ROOT": str(runtime.runtime_root),
        "TURA_PROVIDER_CONFIG": str(runtime.artifacts["provider_config"].path),
        "TURA_GATEWAY_CALLBACKS": "0", "TURA_RUNTIME_AUTO_GIT_COMMIT": "0",
        "TURA_COMMAND_RUN_SANDBOX": "true", "TURA_RUNTIME_ERRORS_FATAL": "1",
        "TURA_SESSION_ACCELERATION_ENABLED": "0", "FORCE_COLOR": "0",
        "TURA_NOKIY_BOUNDED_ONE_TURN": "1",
        "PATH": str(runtime.artifacts["tura_exec"].path.parent) + os.pathsep + env.get("PATH", ""),
    })
    if request.terminal_delivery == "evidence_only":
        env["TURA_NOKIY_EVIDENCE_ONLY_TERMINAL"] = "1"
    if request.initial_task_state is not None:
        initial_state = json.dumps({
            "schema_version": INITIAL_TASK_STATE_SCHEMA,
            "request_sha256": request.request_sha256,
            "session_id": "full-" + request.request_sha256,
            "task_group": request.initial_task_state["task_group"],
            "task_type": request.initial_task_state["task_type"],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(initial_state.encode("utf-8")) > MAX_INITIAL_TASK_STATE_BYTES:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_INITIAL_TASK_STATE_INVALID",
                                           "initial task state envelope exceeds 4096 UTF-8 bytes")
        env[INITIAL_TASK_STATE_ENV] = initial_state
    if request.timeout_seconds is not None:
        env["TURA_EXEC_ROUTER_READ_TIMEOUT_SECS"] = str(request.timeout_seconds)
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
    output = _structured_output(item)
    for result in (item, output):
        if result.get("success") is False or result.get("isError") is True:
            return False
        code = result.get("exit_code")
        if code is not None and (type(code) is not int or code != 0):
            return False
    if item.get("type") == "command_execution":
        receipt = output.get("terminal_receipt")
        if receipt is not None:
            if not isinstance(receipt, dict):
                return False
            if (receipt.get("exit_code") is not None
                    and (type(receipt["exit_code"]) is not int or receipt["exit_code"] != 0)):
                return False
            if receipt.get("outcome") not in (None, "known") or receipt.get("terminal_state") not in (None, "completed"):
                return False
            if any(key in receipt and receipt[key] is not True for key in
                   ("process_reaped", "process_group_empty", "termination_proven")):
                return False
        return any(type(result.get("exit_code")) is int and result["exit_code"] == 0
                   for result in (item, output))
    return True


def _command_evidence(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = deque(maxlen=MAX_COMMAND_EVIDENCE)
    total = 0
    failed = 0
    for index, event in enumerate(items):
        item = event.get("item") if event.get("type") == "item.completed" else None
        if not isinstance(item, dict) or item.get("type") != "command_execution" or not _execution_observation(item):
            continue
        total += 1
        success = _successful_observation(item)
        failed += not success
        output = _structured_output(item)
        codes = [result["exit_code"] for result in (item, output)
                 if type(result.get("exit_code")) is int]
        command = item.get("command")
        records.append({
            "event_index": index,
            "event_sha256": caller._canonical_sha256(event),
            "command_sha256": hashlib.sha256(command.encode()).hexdigest() if isinstance(command, str) else None,
            "exit_code": codes[0] if codes and len(set(codes)) == 1 else None,
            "success": success,
        })
    return {"total_count": total, "failed_count": failed,
            "complete": total <= MAX_COMMAND_EVIDENCE,
            "records": list(records)}


def _provider_observation(summary: Any) -> dict[str, Any]:
    """Only the explicit provider-response summary can supply observed values."""
    empty = {"schema_version": "provider_observation_summary_v1", "source": "provider_response",
             "runtime_count": 0, "observation_count": 0, "conflict_count": 0,
             "model": {"value": None, "observed_count": 0, "distinct_count": 0, "distinct_values": []},
             "service_tier": {"value": None, "observed_count": 0, "distinct_count": 0, "distinct_values": []}}
    def count(value: Any) -> bool:
        return type(value) is int and value >= 0

    def text(value: Any) -> bool:
        return (isinstance(value, str) and 0 < len(value) <= 256 and value.strip() == value
                and not any(ord(ch) < 32 or 127 <= ord(ch) <= 159 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value))

    if not isinstance(summary, dict) or summary.get("schema_version") != empty["schema_version"] or summary.get("source") != "provider_response":
        return empty
    total, observed, conflicts = (summary.get(key) for key in ("runtime_count", "observation_count", "conflict_count"))
    if not all(map(count, (total, observed, conflicts))) or observed + conflicts > total:
        return empty
    result = {**empty, "runtime_count": total, "observation_count": observed, "conflict_count": conflicts}
    for key in ("model", "service_tier"):
        field = summary.get(key)
        if not isinstance(field, dict):
            continue
        seen, distinct, values = (field.get(k) for k in ("observed_count", "distinct_count", "distinct_values"))
        if not count(seen) or seen > observed or not count(distinct) or distinct > seen or not isinstance(values, list) or len(values) > 8 or len(values) > distinct or not all(map(text, values)) or values != sorted(set(values)):
            continue
        singular = field.get("value")
        valid_singular = (total > 0 and observed == total and conflicts == 0 and seen == total and distinct == 1
                          and len(values) == 1 and singular == values[0])
        result[key] = {"value": singular if valid_singular else None, "observed_count": seen,
                       "distinct_count": distinct, "distinct_values": values}
    return result


def _artifact_reference(path: Path, reference: object) -> dict[str, Any]:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256", "bytes"}:
        raise caller.EmbeddedNokiyError("ARTIFACT_REFERENCE_MISSING", "artifact reference required")
    digest, size = reference["sha256"], reference["bytes"]
    # Never resolve here: that would hide a foreign path, traversal or symlink.
    if (reference["path"] != str(path) or not isinstance(digest, str)
            or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            or type(size) is not int or size < 0):
        raise caller.EmbeddedNokiyError("ARTIFACT_REFERENCE_INVALID", "artifact reference differs")
    return reference


def _artifact_stamp(metadata) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


class _ArtifactReader:
    """One no-follow descriptor, incremental hash, and before/after drift proof."""

    def __init__(self, path: Path, *, limit: int | None = None, reference=None):
        self.path, self.limit = path, limit
        self.reference = _artifact_reference(path, reference) if reference is not None else None
        self.digest, self.size, self.eof = hashlib.sha256(), 0, False
        self.record = None

    def __enter__(self):
        self.stream = None
        self.directory = -1
        try:
            parent = self.path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode):
                raise caller.EmbeddedNokiyError("UNSAFE_FILE", "artifact parent is not a physical directory")
            self.directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.parent_identity = (parent.st_dev, parent.st_ino, parent.st_mode)
            opened_parent = os.fstat(self.directory)
            if self.parent_identity != (opened_parent.st_dev, opened_parent.st_ino, opened_parent.st_mode):
                raise caller.EmbeddedNokiyError("ARTIFACT_CHANGED", "artifact parent changed while opening")
            before = os.stat(self.path.name, dir_fd=self.directory, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise caller.EmbeddedNokiyError("UNSAFE_FILE", "not a physical regular artifact")
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=self.directory)
            self.stream = os.fdopen(descriptor, "rb")
            self.opened = os.fstat(self.stream.fileno())
            if not stat.S_ISREG(self.opened.st_mode) or _artifact_stamp(before) != _artifact_stamp(self.opened):
                raise caller.EmbeddedNokiyError("ARTIFACT_CHANGED", "artifact changed while opening")
            if self.limit is not None and self.opened.st_size > self.limit:
                raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED", "artifact exceeds explicit budget")
            if self.reference is not None and self.opened.st_size != self.reference["bytes"]:
                raise caller.EmbeddedNokiyError("ARTIFACT_HASH_MISMATCH", "artifact size differs")
            return self
        except (OSError, caller.EmbeddedNokiyError) as error:
            if self.stream is not None:
                self.stream.close()
            if self.directory != -1:
                os.close(self.directory)
            if isinstance(error, caller.EmbeddedNokiyError):
                raise
            raise caller.EmbeddedNokiyError("FILE_UNAVAILABLE", "artifact cannot be opened") from error

    def _read(self, *, line: bool) -> bytes:
        try:
            raw = self.stream.readline() if line else self.stream.read(65536)
        except OSError as error:
            raise caller.EmbeddedNokiyError("FILE_UNAVAILABLE", "artifact read failed") from error
        self.eof = not raw
        self.size += len(raw)
        self.digest.update(raw)
        if self.limit is not None and self.size > self.limit:
            raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED", "artifact exceeds explicit budget")
        return raw

    def __iter__(self):
        while raw := self._read(line=True):
            yield raw

    def chunks(self):
        while raw := self._read(line=False):
            yield raw

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                if not self.eof:
                    raise caller.EmbeddedNokiyError("ARTIFACT_READ_INCOMPLETE", "artifact was not exhausted")
                after = os.fstat(self.stream.fileno())
                current = os.stat(self.path.name, dir_fd=self.directory, follow_symlinks=False)
                parent = self.path.parent.lstat()
                if (not stat.S_ISREG(current.st_mode)
                        or self.parent_identity != (parent.st_dev, parent.st_ino, parent.st_mode)
                        or _artifact_stamp(after) != _artifact_stamp(self.opened)
                        or _artifact_stamp(current) != _artifact_stamp(self.opened)
                        or self.size != self.opened.st_size):
                    raise caller.EmbeddedNokiyError("ARTIFACT_CHANGED", "artifact drifted during readback")
                self.record = {"path": str(self.path), "sha256": self.digest.hexdigest(), "bytes": self.size}
                if self.reference is not None and self.record != self.reference:
                    raise caller.EmbeddedNokiyError("ARTIFACT_HASH_MISMATCH", "artifact checksum differs")
        except OSError as error:
            raise caller.EmbeddedNokiyError("FILE_UNAVAILABLE", "artifact disappeared during readback") from error
        finally:
            self.stream.close()
            os.close(self.directory)


def _record_artifact(path: Path, *, limit: int | None = None, reference=None) -> dict[str, Any]:
    with _ArtifactReader(path, limit=limit, reference=reference) as source:
        for _ in source.chunks():
            pass
    return source.record


class _Trajectory:
    """Re-iterable disk evidence; indices enumerate all nonblank JSONL events."""

    def __init__(self, path: Path, limit: int | None = None, *, reference=None):
        self.path, self.limit, self.reference = path, limit, reference
        self.count = None
        self.snapshot = None

    def __iter__(self):
        count = 0
        with _ArtifactReader(self.path, limit=self.limit, reference=self.reference) as source:
            snapshot = (source.parent_identity, _artifact_stamp(source.opened))
            if self.snapshot is not None and snapshot != self.snapshot:
                raise caller.EmbeddedNokiyError("ARTIFACT_CHANGED", "trajectory changed between evidence passes")
            for raw in source:
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw.decode("utf-8"), object_pairs_hook=caller._unique_object,
                                       parse_constant=caller._invalid_constant)
                except (ValueError, UnicodeError, RecursionError, caller.EmbeddedNokiyError) as error:
                    raise caller.EmbeddedNokiyError("TRAJECTORY_INVALID_JSON", "invalid trajectory event") from error
                if not isinstance(event, dict):
                    raise caller.EmbeddedNokiyError("TRAJECTORY_INVALID_JSON", "event must be an object")
                count += 1
                yield event
        self.reference, self.count = source.record, count
        self.snapshot = snapshot

    def __len__(self):
        if self.count is None:
            for _ in self:
                pass
        return self.count


def _terminal_identity(request) -> dict[str, Any]:
    fields = request.to_wire() if isinstance(request, caller.EmbeddedNokiyRequest) else request
    return {"model": "codex/" + fields["model"], "agent": fields["execution_profile"],
            "session_id": "full-" + fields["request_sha256"], "cwd": str(fields["workspace"]),
            "reasoning_effort": fields["reasoning_effort"],
            "service_tier": fields.get("service_tier") or ("priority" if fields.get("model_acceleration") else "default")}


class _TerminalEvidence:
    """Validate and project the opt-in marker in the existing trajectory pass."""

    FIELD_NAMES = ("requested_terminal_delivery", "observed_terminal_delivery", "terminal_evidence")
    MARKER_KEYS = {"type", "schema_version", "session_id", "runtime_id", "terminal_status",
                   "delivery_mode", "parent_acceptance_required", "final_summary_turn_executed"}

    def __init__(self, request):
        fields = request.to_wire() if isinstance(request, caller.EmbeddedNokiyRequest) else (request or {})
        mode = fields.get("terminal_delivery", "assistant_reply")
        if "terminal_delivery" in fields and (
                not isinstance(mode, str) or mode not in {"assistant_reply", "evidence_only"}
                or fields.get("schema_version") != caller.REQUEST_SCHEMA_VERSION
                or fields.get("execution_profile") not in {"direct", "balanced"}):
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID", "unsupported delivery request")
        self.enabled = mode == "evidence_only"
        self.session_id = "full-" + fields["request_sha256"] if self.enabled else None
        self.marker = None

    def _validate_marker(self, event):
        if (not self.enabled or not isinstance(event, dict) or set(event) != self.MARKER_KEYS
                or event.get("type") != "nokiy.terminal_evidence"
                or event.get("schema_version") != "nokiy_terminal_evidence_v1"
                or event.get("session_id") != self.session_id
                or not isinstance(event.get("runtime_id"), str) or not event["runtime_id"].strip()
                or event.get("terminal_status") not in ("done", "blocked") or event.get("delivery_mode") != "evidence_only"
                or event.get("parent_acceptance_required") is not True
                or event.get("final_summary_turn_executed") is not False):
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID", "invalid or unrequested terminal marker")

    def observe(self, event):
        if not isinstance(event, dict):
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID", "invalid terminal event")
        kind = event.get("type")
        if kind == "nokiy.terminal_evidence" or event.get("schema_version") == "nokiy_terminal_evidence_v1":
            self._validate_marker(event)
            if self.marker is not None:
                raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID", "duplicate terminal marker")
            self.marker = dict(event)
        elif self.marker is not None and (
                isinstance(kind, str) and (kind.startswith(("item.", "assistant.", "tool."))
                                          or kind in {"assistant_message", "tool_call", "tool_result"})):
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID", "assistant/tool event after terminal marker")

    def result_text(self):
        if self.marker is None:
            return None
        return caller._canonical_bytes({key: self.marker[key] for key in (
            "terminal_status", "delivery_mode", "parent_acceptance_required", "final_summary_turn_executed"
        )}).decode("utf-8")

    def fields(self):
        if not self.enabled:
            return {}
        return {"requested_terminal_delivery": "evidence_only",
                "observed_terminal_delivery": "evidence_only" if self.marker is not None else "assistant_reply",
                "terminal_evidence": self.marker}

    def validate_projection(self, value):
        actual = {key: value[key] for key in self.FIELD_NAMES if key in value}
        if actual.get("terminal_evidence") is not None:
            self._validate_marker(actual["terminal_evidence"])
        if actual != self.fields():
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_EVIDENCE_INVALID", "terminal delivery projection differs")


class _TurnSummary:
    def __init__(self, expected=None, *, require_completed=True):
        self.expected, self.count, self.usage = expected, 0, None
        self.require_completed = require_completed
        self.provider_observation = _provider_observation(None)

    def observe(self, event):
        if event.get("type") != "turn.completed":
            return
        self.count += 1
        if self.count != 1:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_INVALID", "duplicate completed turn")
        if self.require_completed and event.get("status") != "completed":
            raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED", "turn not completed")
        if self.expected is not None and any(event.get(k) != v for k, v in self.expected.items()):
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_IDENTITY_MISMATCH", "CLI terminal binding differs")
        usage = event.get("usage")
        if len(caller._canonical_bytes(usage)) > 4096:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_SUMMARY_LIMIT_EXCEEDED", "usage summary too large")
        self.usage = usage
        self.provider_observation = _provider_observation(event.get("provider_observation"))

    def finish(self):
        if self.count != 1:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_TERMINAL_INVALID", "expected exactly one completed turn")


def _text_preview(text: str, limit: int) -> tuple[str, bool]:
    # Preserve the existing UTF-8 head/tail preview without encoding all text.
    bounded = text if len(text) <= limit else text[:limit] + text[-limit:]
    return caller._bounded_preview(bounded, limit)


def _text_chunks(text: str):
    for offset in range(0, len(text), 16384):
        yield text[offset:offset + 16384].encode("utf-8")


def _message_reference(path: Path, text: str) -> dict[str, Any]:
    digest, size = hashlib.sha256(), 0
    for raw in _text_chunks(text):
        digest.update(raw)
        size += len(raw)
    return {"path": str(path), "sha256": digest.hexdigest(), "bytes": size}


def _result_projection(run: Path, preview: object, truncated: object, reference: object,
                       limit: int) -> tuple[str, bool, dict[str, Any] | None]:
    if (not isinstance(preview, str) or type(truncated) is not bool
            or len(preview.encode("utf-8")) > limit):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_RESULT_INVALID", "result projection is not bounded")
    if not truncated:
        if reference is not None:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_RESULT_INVALID", "unexpected result artifact")
        return preview, False, None
    path = run / "last-message.txt"
    reference = _artifact_reference(path, reference)
    head, tail = b"", b""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with _ArtifactReader(path, reference=reference) as source:
            for raw in source.chunks():
                decoder.decode(raw)
                head += raw[:max(0, limit - len(head))]
                tail = (tail + raw)[-limit:]
            decoder.decode(b"", final=True)
    except UnicodeError as error:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_RESULT_INVALID", "result artifact is not UTF-8") from error
    actual, was_truncated = caller._bounded_preview(
        head.decode("utf-8", errors="ignore") + tail.decode("utf-8", errors="ignore"), limit)
    if source.size <= limit or not was_truncated or actual != preview:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_RESULT_INVALID", "result artifact and preview differ")
    return preview, True, source.record


def _events(path: Path, limit: int | None, request: caller.EmbeddedNokiyRequest) -> dict[str, Any]:
    items = _Trajectory(path, limit)
    turn = _TurnSummary(_terminal_identity(request))
    delivery = _TerminalEvidence(request)
    preview, truncated, result_ref = "", False, None
    tools = successful = 0
    result_path = path.parent / "last-message.txt"
    output = None
    with ExitStack() as stack:
        def observations():
            nonlocal preview, truncated, result_ref, tools, successful, output
            for event in items:
                delivery.observe(event)
                turn.observe(event)
                item = event.get("item") if event.get("type") == "item.completed" else None
                if isinstance(item, dict) and item.get("type") == "assistant_message":
                    text = item.get("text", "")
                    if not isinstance(text, str):
                        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_RESULT_INVALID", "assistant text must be a string")
                    preview, truncated = _text_preview(text, request.max_result_bytes)
                    if truncated and output is None:
                        output = stack.enter_context(result_path.open("xb"))
                    if output is not None:
                        output.seek(0)
                        output.truncate()
                        digest, size = hashlib.sha256(), 0
                        for raw in _text_chunks(text):
                            output.write(raw)
                            digest.update(raw)
                            size += len(raw)
                        output.flush()
                        result_ref = {"path": str(result_path), "sha256": digest.hexdigest(), "bytes": size}
                    # Do not retain the last large message while parsing later events.
                    del text
                if isinstance(item, dict) and _execution_observation(item):
                    tools += 1
                    successful += _successful_observation(item)
                yield event
        evidence = _command_evidence(observations())
        turn.finish()
        evidence_text = delivery.result_text()
        if evidence_text is not None:
            preview, truncated, result_ref = evidence_text, False, None
        if output is not None:
            os.fsync(output.fileno())
            if _artifact_stamp(os.fstat(output.fileno())) != _artifact_stamp(result_path.lstat()):
                raise caller.EmbeddedNokiyError("ARTIFACT_CHANGED", "result artifact was replaced")
        if truncated:
            result_ref = _record_artifact(result_path, reference=result_ref)
    return {"final_text": preview, "result_truncated": truncated,
            "result_artifact": result_ref if truncated else None,
            "trajectory_artifact": items.reference, "usage": turn.usage,
            "provider_observation": turn.provider_observation,
            "tool_loop": {"completed_count": tools, "successful_count": successful},
            "command_evidence": evidence, "turn_completed": True, **delivery.fields()}


class _PhaseTiming:
    """Fixed-size, diagnostic-only checkpoints; never retry a failed publication."""

    def __init__(self, request, run: Path, started_at: float):
        self.run, self.started_at, self.writable = run, started_at, True
        self.observation = {
            "schema_version": PHASE_TIMING_SCHEMA,
            "request_id": request.request_id, "request_sha256": request.request_sha256,
            "checkpoints_ms": {"engine_started": 0},
        }
        self._publish()

    def observe(self, checkpoint: str) -> None:
        self.observation["checkpoints_ms"][checkpoint] = round((time.monotonic() - self.started_at) * 1000)
        self._publish()

    def _publish(self) -> None:
        if not self.writable:
            return
        try:
            data = caller._canonical_bytes(self.observation)
            if len(data) > MAX_PHASE_TIMING_BYTES:
                self.writable = False
                return
            # A killed writer leaves either the previous snapshot or the new one,
            # plus at most one bounded temporary file. Never truncate in place.
            pending = self.run / "phase-timing.pending"
            with pending.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, self.run / "phase-timing.json")
            fd = os.open(self.run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # Diagnostics must not replace the first execution/cleanup failure.
            self.writable = False


def _valid_phase_timing(value, request) -> dict[str, Any] | None:
    if (not isinstance(value, dict) or value.get("schema_version") != PHASE_TIMING_SCHEMA
            or value.get("request_id") != request.request_id
            or value.get("request_sha256") != request.request_sha256):
        return None
    marks = value.get("checkpoints_ms")
    if (not isinstance(marks, dict) or not marks or set(marks) - set(_PHASE_CHECKPOINTS)
            or marks.get("engine_started") != 0
            or any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in marks.values())):
        return None
    # Normal phases form a prefix; cleanup may follow any interrupted phase.
    previous = 0
    for index, name in enumerate(_PHASE_CHECKPOINTS):
        if name not in marks:
            continue
        if marks[name] < previous:
            return None
        if index and name != "cleanup_started" and _PHASE_CHECKPOINTS[index - 1] not in marks:
            return None
        previous = marks[name]
    return {"schema_version": PHASE_TIMING_SCHEMA,
            "request_id": request.request_id, "request_sha256": request.request_sha256,
            "checkpoints_ms": dict(marks)}


def _recover_phase_timing(run: Path, request, engine: dict[str, Any]) -> dict[str, Any] | None:
    observed = _valid_phase_timing(engine.get("phase_timing_observation"), request)
    if observed is not None:
        return observed
    try:
        # No symlinks, special files, unbounded reads, or temporary-file recovery.
        fd = os.open(run / "phase-timing.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return None
            data = source.read(MAX_PHASE_TIMING_BYTES + 1)
        if len(data) > MAX_PHASE_TIMING_BYTES:
            return None
        value = json.loads(data, object_pairs_hook=caller._unique_object,
                           parse_constant=caller._invalid_constant)
        return _valid_phase_timing(value, request)
    except (OSError, ValueError, RecursionError, caller.EmbeddedNokiyError):
        return None


def _phase_intervals(observed: dict[str, Any] | None) -> dict[str, int | None]:
    # Null means unknown/incomplete, not zero. Never extrapolate to parent time;
    # engine cleanup observations are not proof of supervisor descendant cleanup.
    marks = observed["checkpoints_ms"] if observed is not None else {}
    pairs = {name: (_PHASE_CHECKPOINTS[index - 1], name)
             for index, name in enumerate(_PHASE_CHECKPOINTS[1:7], 1)}
    cleanup_start = "trajectory_parse" if "trajectory_parse" in marks else "cleanup_started"
    pairs.update(cleanup=(cleanup_start, "cleanup"), engine_total=("engine_started", "cleanup"))
    return {name: marks[end] - marks[start] if start in marks and end in marks else None
            for name, (start, end) in pairs.items()}


def _engine(spec_path: Path) -> dict[str, Any]:
    started_at = time.monotonic()
    spec = caller._load_json(spec_path, limit=caller.MAX_REQUEST_BYTES, code="NOKIY_FULL_CORE_SPEC_INVALID")
    request = caller.decode_request(spec["request"])
    run = spec_path.parent
    timing = _PhaseTiming(request, run, started_at)
    processes: list[subprocess.Popen] = []
    logs = []
    result: dict[str, Any] = {}
    try:
        # Inputs are checked again after entering the isolated process scope.
        ready, runtime, _, _ = prepare(request)
        if ready["status"] != "READY":
            raise caller.EmbeddedNokiyError(ready["first_typed_blocker"],
                                            "required router capability unavailable")
        timing.observe("prepare")
        state = run / "execution-state"
        state.mkdir(mode=0o700)
        env = _environment(request, runtime, state, execution_budget=os.environ.get(BUDGET_ENV))
        # Same CLI store-open path as the router's runtime calls. Fail before
        # starting services or invoking the provider if the binding is invalid.
        try:
            preflight = subprocess.run([str(runtime.artifacts["tura_router"].path),
                "command-receipt-preflight"], cwd=request.workspace, env=env,
                capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_COMMAND_RECEIPT_STORE_INVALID",
                                            "request-bound receipt store cannot open") from error
        if preflight.returncode != 0 or preflight.stdout:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_COMMAND_RECEIPT_STORE_INVALID",
                                            "request-bound receipt store cannot open")
        address = None
        for name, arguments, marker in (
            ("tura_session_db", [], "service.addr"),
            ("tura_router", ["serve-socket"], "router.addr"),
        ):
            log = (run / (name + ".log")).open("xb")
            logs.append(log)
            fds = _verifier_fd() if name == "tura_router" else ()
            service_env = dict(env)
            if fds:
                service_env["NOKIY_VERIFIER_FD"] = str(fds[0])
            process = subprocess.Popen([str(runtime.artifacts[name].path), *arguments],
                                       cwd=request.workspace, env=service_env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, pass_fds=fds)
            for fd in fds:
                os.close(fd)
            processes.append(process)
            deadline = time.monotonic() + (15 if request.timeout_seconds is None else min(15, request.timeout_seconds))
            endpoint_path = state / "session_log" / marker
            while not endpoint_path.is_file():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_SERVICE_NOT_READY", name)
                time.sleep(.05)
            endpoint = caller._load_json(endpoint_path, limit=16384, code="NOKIY_FULL_CORE_ENDPOINT_INVALID")
            _verify_service_owner(name, endpoint, process, state)
            if name == "tura_session_db":
                timing.observe("session_db_ready")
            if name == "tura_router":
                address = endpoint.get("addr")
                if not isinstance(address, str) or not address.startswith("127.0.0.1:"):
                    raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_ENDPOINT_INVALID", name)
                env["TURA_ROUTER_ADDR"] = address
                timing.observe("router_ready")
        trajectory = run / "core.jsonl"
        with (run / "prompt.txt").open("rb") as prompt, trajectory.open("xb") as output, (run / "core.stderr").open("xb") as errors:
            process = subprocess.Popen(_cli_argv(request, runtime, run, address),
                                       cwd=request.workspace, env=env, stdin=prompt, stdout=output, stderr=errors)
            processes.append(process)
            timing.observe("cli_launch")
            while process.poll() is None:
                if ((request.max_trajectory_bytes is not None
                     and trajectory.stat().st_size > request.max_trajectory_bytes)
                        or errors.tell() > 4 * 1024 * 1024):
                    raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED", "CLI output budget")
                if any(log.tell() > 4 * 1024 * 1024 for log in logs):
                    raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_SERVICE_LOG_LIMIT", "service log budget")
                time.sleep(.05)
            timing.observe("cli_process")
            if process.returncode != 0:
                raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED", "CLI exited unsuccessfully")
        result = _events(trajectory, request.max_trajectory_bytes, request)
        timing.observe("trajectory_parse")
    finally:
        original_error = sys.exc_info()[1]
        cleanup_error = None
        timing.observe("cleanup_started")
        # Stop handles we created, never rediscover services by name or persisted PID.
        for process in reversed(processes):
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as error:
                cleanup_error = cleanup_error or error
        for log in logs:
            try:
                log.close()
            except OSError as error:
                cleanup_error = cleanup_error or error
        if cleanup_error is None:
            timing.observe("cleanup")
        elif original_error is None:
            raise cleanup_error
    result["phase_timing_observation"] = timing.observation
    result["phase_timings_ms"] = _phase_intervals(timing.observation)
    return result


def execute_full_core(request: caller.EmbeddedNokiyRequest) -> dict[str, Any]:
    caller._verify_native_thread_binding(request)
    run = request.artifact_root / request.request_id
    if run.is_symlink():
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_UNCERTAIN_PRIOR_ATTEMPT", "symlink run directory")
    if (run / "terminal.json").is_file():
        return caller.read_terminal(request.artifact_root, request.request_id)
    ready, runtime, capsule, jspace = prepare(request)
    if ready["status"] != "READY":
        raise caller.EmbeddedNokiyError(ready["first_typed_blocker"],
                                      "scoped verifier cannot start inside the inherited process fence")
    try:
        run.mkdir(mode=0o700)
    except FileExistsError as error:
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_UNCERTAIN_PRIOR_ATTEMPT", "run exists without terminal") from error
    caller._write_create_only(run / "request-identity.json", request.to_wire())
    caller._write_create_only(run / "capsule.json", capsule)
    caller._write_create_only(run / "jspace.json", jspace)
    # Retain the exact input bytes: the request reference hashes the source
    # artifact, not the canonicalized copy above.
    with request.jspace_contract.path.open("rb") as source:
        contract_raw = source.read(caller.MAX_CONTEXT_BYTES + 1)
    if (len(contract_raw) > caller.MAX_CONTEXT_BYTES
            or hashlib.sha256(contract_raw).hexdigest() != request.jspace_contract.sha256):
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_JSPACE_DRIFT", "contract changed before capture")
    with (run / "jspace-original.json").open("xb") as output:
        output.write(contract_raw)
        output.flush()
        os.fsync(output.fileno())
    caller._write_create_only(run / "execution.json", {"request": request.to_wire(include_identity=False)})
    with (run / "prompt.txt").open("xb") as output:
        output.write(_provider_prompt(request.prompt, jspace,
                                     terminal_delivery=request.terminal_delivery).encode())
    # Add a signal-only fence; do not relax inherited filesystem/network policy.
    # Actual tool writes remain under the full core's sandbox and J-Space contract.
    fence = "(version 1) (allow default) (deny signal) (allow signal (target same-sandbox))"
    command = ["/usr/bin/sandbox-exec", "-p", fence, sys.executable, "-B", "-m",
               MODULE, "--supervise", str(run / "execution.json"), "--parent-pid", str(os.getpid())]
    trajectory, errors = run / "supervision.json", run / "supervision.stderr"
    engine, scope = {}, {}
    supervision_reference = trajectory_reference = None
    failure = None
    wall = 0.0
    code = -1
    channel = verifier = None
    env = _scrub_receipt_binding(os.environ)
    env.pop("NOKIY_VERIFIER_FD", None)
    kwargs = {}
    try:
        if jspace.get("verifier_commands"):
            from .verifier_channel import VerifierChannel
            from .verifier_parent import ParentVerifier
            deadline = None if request.timeout_seconds is None else time.monotonic() + request.timeout_seconds
            # prepare/_verify_context already checked authority/content digests
            # and live DCF freshness for precisely this action-scoped generation.
            generation = capsule.get("dcf_generation", {})
            validated_dcf_generation = (generation
                if jspace.get("schema_version") == "jspace_contract_v2"
                and ready.get("context", {}).get("context_mode") == "dcf_jspace_required"
                and "action_freshness" in generation else None)
            verifier = ParentVerifier(request.workspace, jspace, runtime.artifacts["tura_router"],
                                      deadline, validated_dcf_generation=validated_dcf_generation)
            # Transport/handler limits are per operation, never an idle lifetime.
            channel_timeout = (max(grant["timeout_seconds"] for grant in jspace["verifier_commands"])
                               if request.timeout_seconds is None else request.timeout_seconds) + 20
            channel = VerifierChannel(verifier.binding, len(jspace["verifier_commands"]), verifier,
                                      timeout_seconds=channel_timeout).__enter__()
            env["NOKIY_VERIFIER_FD"] = str(channel.router_fd)
            kwargs = {"pass_fds":(channel.router_fd,), "on_spawn":channel.release_router_copy}
        code, wall, failure, _ = caller._run_process(command, cwd=request.workspace,
            env=env, stdin_path=run / "prompt.txt", stdout_path=trajectory,
            stderr_path=errors, timeout=None if request.timeout_seconds is None else request.timeout_seconds + 30,
            output_limit=MAX_SUPERVISION_BYTES, **kwargs)
        outcome, supervision_raw = caller._load_json_snapshot(
            trajectory, limit=MAX_SUPERVISION_BYTES, code="NOKIY_FULL_CORE_SUPERVISION_INVALID")
        supervision_reference = {"path": str(trajectory),
                                 "sha256": hashlib.sha256(supervision_raw).hexdigest(),
                                 "bytes": len(supervision_raw)}
        del supervision_raw
        _record_artifact(trajectory, limit=MAX_SUPERVISION_BYTES, reference=supervision_reference)
        engine, scope = outcome.get("result") or {}, outcome.get("scope") or {}
        if not isinstance(engine, dict) or not isinstance(scope, dict):
            engine, scope = {}, {}
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_SUPERVISION_INVALID", "invalid supervision summary")
        failure = failure or outcome.get("failure")
        if code != 0:
            failure = failure or "NOKIY_FULL_CORE_EXECUTION_FAILED"
        if failure is None and engine.get("turn_completed") is not True:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_SUPERVISION_INVALID", "completed engine summary required")
    except (OSError, ValueError, subprocess.SubprocessError, caller.EmbeddedNokiyError) as error:
        failure = failure or getattr(error, "code", "NOKIY_FULL_CORE_SUPERVISION_FAILED")
    finally:
        if channel is not None:
            try:
                channel.close()
            except RuntimeError:
                failure = failure or "NOKIY_VERIFIER_CHANNEL_CLEANUP_UNPROVEN"
            failure = failure or channel.failure
    cleanup = scope.get("engine_reaped") is True and scope.get("no_live_descendants") is True and not scope.get("cleanup_error")
    if verifier is not None:
        scope["parent_verifier"] = {"calls":verifier.calls, "cleanup_pass":verifier.cleanup_pass,
                                    "channel_failure":(channel.failure or "")[:2048] if channel else None}
        cleanup = cleanup and verifier.cleanup_pass
    if not cleanup:
        failure = failure or "NOKIY_EMBEDDED_CLEANUP_UNPROVEN"
    if request.require_tool_call and engine.get("tool_loop", {}).get("successful_count", 0) == 0:
        failure = failure or "NOKIY_EMBEDDED_TOOL_LOOP_NOT_OBSERVED"
    from .source_excerpt import verify_context_excerpt
    source_context_readback = None
    try:
        source_context_readback = verify_context_excerpt(
            request.workspace, capsule, jspace, after_execution=True)
    except caller.EmbeddedNokiyError as error:
        failure = failure or error.code
    preview, truncated, result_artifact = "", False, None
    try:
        preview, truncated, result_artifact = _result_projection(
            run, engine.get("final_text", ""), engine.get("result_truncated", False),
            engine.get("result_artifact"), request.max_result_bytes)
    except (OSError, UnicodeError, caller.EmbeddedNokiyError) as error:
        failure = failure or getattr(error, "code", "NOKIY_FULL_CORE_RESULT_INVALID")
    if not preview:
        failure = failure or "NOKIY_EMBEDDED_RESULT_MISSING"
    delivery = _TerminalEvidence(request)
    try:
        if engine.get("turn_completed") is True:
            if engine.get("terminal_evidence") is not None:
                delivery.observe(engine["terminal_evidence"])
            delivery.validate_projection(engine)
            evidence_text = delivery.result_text()
            if evidence_text is not None and (preview != evidence_text or truncated or result_artifact is not None):
                raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_RESULT_INVALID", "terminal marker result differs")
    except caller.EmbeddedNokiyError as error:
        failure = failure or error.code
        delivery = _TerminalEvidence(request)
    command_evidence = engine.get("command_evidence")
    evidence_artifact = None
    if isinstance(command_evidence, dict):
        evidence_path = run / "command-evidence.json"
        caller._write_create_only(evidence_path, command_evidence)
        evidence_artifact = caller._record(evidence_path)
    file_evidence = None
    file_artifact = None
    core_path = run / "core.jsonl"
    if core_path.exists() or core_path.is_symlink() or engine.get("turn_completed") is True:
        try:
            expected_core = (_artifact_reference(core_path, engine.get("trajectory_artifact"))
                             if engine.get("turn_completed") is True else None)
            trajectory_reference = _record_artifact(core_path, reference=expected_core)
            events = _Trajectory(core_path, request.max_trajectory_bytes, reference=trajectory_reference)
            file_evidence = file_change_evidence.produce(events, request, jspace)
            if file_evidence is not None:
                file_path = run / "file-change-evidence.json"
                caller._write_create_only(file_path, file_evidence)
                file_artifact = caller._record(file_path)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError,
                caller.EmbeddedNokiyError) as error:
            failure = failure or (str(error) if isinstance(error, file_change_evidence.EffectError)
                                  else getattr(error, "code", "FILE_CHANGE_CAPTURE_UNAVAILABLE"))
    # Closeout binds the same physical evidence read by the engine and parent.
    for artifact_path, reference, artifact_limit in (
            (core_path, trajectory_reference, None),
            (trajectory, supervision_reference, MAX_SUPERVISION_BYTES)):
        try:
            if reference is not None:
                _record_artifact(artifact_path, limit=artifact_limit, reference=reference)
            elif artifact_path == trajectory and (artifact_path.exists() or artifact_path.is_symlink()):
                supervision_reference = _record_artifact(artifact_path, limit=artifact_limit)
        except caller.EmbeddedNokiyError as error:
            failure = failure or error.code
    provider_observation = _provider_observation(engine.get("provider_observation"))
    phase_observation = _recover_phase_timing(run, request, engine)
    receipt = {
        "schema_version": caller.TERMINAL_SCHEMA_VERSION,
        "executor_name": EXECUTOR_NAME,
        "request_id": request.request_id, "request_sha256": request.request_sha256,
        "status": "BLOCKED" if failure else "RESULT_AVAILABLE", "first_typed_blocker": failure,
        "runtime_image_sha256": runtime.image_sha256, "runtime_build_identity": runtime.build_identity,
        "execution_model": "single_task_full_core", "execution_profile": request.execution_profile,
        "native_thread_id": request.native_thread_id, "continuation_owner": "codex", "mission_acceptance": "parent_owned",
        "model": request.model, "reasoning_effort": request.reasoning_effort,
        "requested_service_tier": ready["requested_service_tier"],
        "observed_model": provider_observation["model"]["value"],
        "observed_service_tier": provider_observation["service_tier"]["value"],
        "provider_observation": provider_observation,
        "result_text": preview, "result_truncated": truncated, "result_artifact": result_artifact,
        "usage": engine.get("usage"),
        "phase_timings_ms": _phase_intervals(phase_observation),
        "phase_timing_observation": phase_observation,
        "tool_loop": engine.get("tool_loop"), "wall_time_seconds": wall, "runtime_exit_code": code,
        "command_evidence_artifact": evidence_artifact,
        "command_evidence_summary": ({key: command_evidence[key] for key in
                                      ("total_count", "failed_count", "complete")}
                                     if isinstance(command_evidence, dict) else None),
        "cleanup": scope, "cleanup_pass": cleanup, "fallback_used": False,
        "replayable_terminal": True, "preflight_sha256": caller._canonical_sha256(ready),
        "trajectory_artifact": trajectory_reference,
        "supervision_artifact": supervision_reference,
        **delivery.fields(),
    }
    if file_artifact is not None:
        receipt["file_change_evidence_artifact"] = file_artifact
        receipt["original_jspace_artifact"] = caller._record(run / "jspace-original.json")
        receipt["file_change_evidence_summary"] = {
            "total_count": file_evidence["total_count"], "target_count": file_evidence["target_count"]}
    if source_context_readback is not None:
        receipt["source_context_readback"] = source_context_readback
    if len(caller._canonical_bytes(receipt)) > caller.MAX_TERMINAL_BYTES:
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_TERMINAL_TOO_LARGE", "full-core terminal too large")
    caller._write_create_only(run / "terminal.json", receipt)
    return receipt


def _summary_json(value: dict[str, Any], limit: int) -> str:
    encoded = json.dumps(value).encode("utf-8")
    if len(encoded) + 1 > limit:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_SUMMARY_LIMIT_EXCEEDED", "supervision summary too large")
    return encoded.decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--engine", type=Path)
    mode.add_argument("--supervise", type=Path)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    if args.engine:
        try:
            print(_summary_json(_engine(args.engine), MAX_ENGINE_SUMMARY_BYTES))
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
            cwd=str(request.workspace), env=_supervisor_environment(request), input_bytes=b"", timeout=request.timeout_seconds,
            cancelled=cancelled, max_stdout=MAX_ENGINE_SUMMARY_BYTES, max_stderr=65536,
            parent_pid=args.parent_pid, pass_fds=_verifier_fd())
        result = json.loads(outcome.stdout, object_pairs_hook=caller._unique_object,
                            parse_constant=caller._invalid_constant) if outcome.stdout else {}
        failure = outcome.failure or result.get("failure")
        if outcome.returncode != 0:
            failure = failure or "NOKIY_FULL_CORE_ENGINE_FAILED"
        print(_summary_json({"result": result, "scope": outcome.scope, "failure": failure}, MAX_SUPERVISION_BYTES))
        return 2 if failure else 0
    except Exception as error:
        print(json.dumps({"failure": getattr(error, "code", type(error).__name__), "scope": {}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
