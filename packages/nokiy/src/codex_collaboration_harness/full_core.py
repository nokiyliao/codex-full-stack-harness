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
from types import SimpleNamespace

from . import embedded_nokiy as caller
from . import file_change_evidence
from .graph_process import ProcessScopeError, current_process_is_sandboxed, inherited_root_parent, supervise

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
SOURCE_DIGEST_CAPABILITY = b"nokiy_admitted_source_digest_v1"
CAPSULE_DIGEST_CAPABILITY = b"nokiy_capsule_source_digest_v1"
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
            if not isinstance(pin, dict) or pin.get("scope") == "verifier_only":
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


def _admitted_digest_paths(jspace: dict[str, Any]) -> list[str]:
    if jspace.get("schema_version") != "jspace_contract_v2":
        return []
    verifiers = jspace.get("verifier_commands")
    if not isinstance(verifiers, list) or not all(isinstance(item, dict) for item in verifiers):
        return []
    mapped = jspace.get("mapped_source_reads", [])
    if not isinstance(mapped, list) or not all(isinstance(item, dict) for item in mapped):
        return []
    aliases = {item.get(key) for item in mapped
               for key in ("logical_path", "requested_logical_path")
               if isinstance(item.get(key), str)}
    return [path for path in _known_source_sha256_by_path(jspace, verifiers)
            if path not in aliases]


def _supports_admitted_digest(runtime, jspace: dict[str, Any], environment: dict[str, str]) -> bool:
    if not _admitted_digest_paths(jspace):
        return False
    try:
        result = subprocess.run([str(runtime.artifacts["tura_router"].path),
                                 "source-read-capabilities"],
                                capture_output=True, timeout=10, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == SOURCE_DIGEST_CAPABILITY + b"\n"


def _observed_digest_eligible(jspace: dict[str, Any]) -> bool:
    operations = jspace.get("allowed_operations")
    denied = jspace.get("denied_operations", [])
    return (jspace.get("schema_version") == "jspace_contract_v2"
            and jspace.get("source_read") is True
            and isinstance(operations, list) and "read" in operations and "command" in operations
            and isinstance(denied, list) and all(isinstance(item, str) for item in denied)
            and not {"read", "command"}.intersection(denied)
            and jspace.get("write_scopes") == []
            and jspace.get("mapped_source_reads", []) == [])


def _capsule_digest_paths(capsule, jspace):
    from .source_excerpt import readonly_outline_pins
    if not _observed_digest_eligible(jspace):
        return []
    return list(readonly_outline_pins(capsule, jspace))


def _supports_capsule_digest(runtime, capsule, jspace, environment):
    if not _capsule_digest_paths(capsule, jspace):
        return False
    try:
        result = subprocess.run([str(runtime.artifacts["tura_router"].path),
                                 "capsule-source-read-capabilities"],
                                capture_output=True, timeout=10, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == CAPSULE_DIGEST_CAPABILITY + b"\n"


def _supports_observed_digest(runtime, jspace: dict[str, Any], environment: dict[str, str]) -> bool:
    if not _observed_digest_eligible(jspace):
        return False
    try:
        result = subprocess.run([str(runtime.artifacts["tura_router"].path),
                                 "observed-source-read-capabilities"],
                                capture_output=True, timeout=10, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == b"nokiy_observed_source_digest_v1\n"


def _supports_directory_source_read(runtime, jspace: dict[str, Any], environment: dict[str, str]) -> bool:
    if (jspace.get("schema_version") != "jspace_contract_v2"
            or _directory_read_presentation(jspace) is None):
        return False
    try:
        result = subprocess.run([str(runtime.artifacts["tura_router"].path),
                                 "directory-source-read-capabilities"],
                                capture_output=True, timeout=10, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == b"nokiy_directory_source_read_v1\n"


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
                     terminal_delivery: str = "assistant_reply", *,
                     admitted_digest_reference: bool = False,
                     observed_digest_reference: bool = False,
                     directory_source_read: bool = False,
                     capsule: dict | None = None,
                     capsule_digest_reference: bool = False,
                     workspace: str | None = None) -> str:
    """Present verified tool capabilities, without changing their enforcement."""
    if terminal_delivery == "evidence_only":
        prompt += _EVIDENCE_ONLY_COMPLETION_GUIDANCE
    execution_workspace = _exact_presentation_path(workspace, absolute=True)
    operations = jspace.get("allowed_operations", [])
    command_allowed = isinstance(operations, list) and "command" in operations
    inspect_shell = jspace.get("inspect_shell")
    if command_allowed and "read" in operations and isinstance(inspect_shell, dict):
        projection = {
            "jspace_semantic_sha256": jspace.get("authorization_semantic_sha256"),
            "allowed_operations": operations, "denied_operations": jspace.get("denied_operations", []),
            "inspect_shell": {key: inspect_shell.get(key) for key in ("evidence_root", "network")},
        }
        return _capability_gap_prompt(
            prompt + "\n\nVerified execution capability (caller projection):\n"
            + json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            + "\nInspect shell: command_run shell lines of any form (pipelines, heredocs, python3 -c, git, rg, "
              "curl to localhost) run inside an OS sandbox: reads except credential stores, writes only under "
              "evidence_root and $TMPDIR, network only to localhost when network is loopback. Repository writes "
              "and apply_patch fail; a sandbox denial is a boundary, not a path to retry. Write and edit "
              "task-local files (scripts, notes, evidence) with shell lines under evidence_root, also where the "
              "task names apply_patch: that is this action's admitted route, not a switched path. For multi-step "
              "analysis, write shared setup (paths, imports, loaders, helpers) once to a module under evidence_root "
              "and import it in later steps instead of retyping it: command text is model output, the slowest part "
              "of a run. A command that "
              "exits 0 despite a reported denied write (such as a CLI's alias or cache setup) still produced its "
              "output. Batch related checks into few command lines, open SQLite as "
              "file:<path>?mode=ro with PRAGMA query_only=ON so committed WAL data remains visible. "
              "Use immutable=1 only for a verified immutable snapshot, never a live database. "
              "If the sandbox cannot read the live WAL state, report unavailable evidence rather than "
              "retrying with immutable=1 or treating missing rows as zero. "
              "Finish with findings that cite evidence paths.\n")
    read_allowed = (command_allowed and jspace.get("source_read") is True
                    and "read" in operations)
    parent_shell = jspace.get("parent_shell")
    if command_allowed and "read" in operations and isinstance(parent_shell, dict):
        from .parent_shell import NOTICE as parent_shell_notice
        prompt += parent_shell_notice + f" Shell evidence root: {parent_shell.get('evidence_root')}.\n"
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
        if directory_source_read is True and jspace.get("schema_version") == "jspace_contract_v2":
            projection["directory_source_read"] = {
                "roots": directory_routes["roots"], "authority": "read_commands.cat",
            }
    if read_allowed:
        projection.update(schema_version="nokiy_source_read_presentation_v1",
                          source_read=True, read_scopes=jspace.get("read_scopes", []))
        source_root = _exact_presentation_path(jspace.get("repo_root"), absolute=True)
        if execution_workspace is not None and source_root is not None:
            projection["source_read_root"] = source_root
        if observed_digest_reference is True and _observed_digest_eligible(jspace):
            projection["observed_source_digest_reference"] = True
        if capsule_digest_reference is True:
            paths = _capsule_digest_paths(capsule, jspace)
            if paths:
                projection["admitted_source_digest_paths"] = paths
        if verifier_allowed:
            known = _known_source_sha256_by_path(jspace, verifiers)
            if known:
                projection["known_source_sha256_by_path"] = known
            if admitted_digest_reference is True:
                referenced = _admitted_digest_paths(jspace)
                if referenced:
                    projection["admitted_source_digest_paths"] = referenced
                    remaining = {path: digest for path, digest in known.items()
                                 if path not in referenced}
                    if remaining:
                        projection["known_source_sha256_by_path"] = remaining
                    else:
                        projection.pop("known_source_sha256_by_path", None)
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
                arguments = [path, 1, 80]
                known = projection.get("known_source_sha256_by_path", {})
                if path in known:
                    arguments.append(known[path])
                if path in projection.get("admitted_source_digest_paths", []):
                    arguments = {"path": path, "start_line": 1, "end_line": 80,
                                 "expected_sha256_ref": "admitted"}
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
        contexts = []
        for index, command in enumerate(verifiers if execution_workspace is not None else []):
            context = {"verifier_index": index}
            cwd = _exact_presentation_path(command.get("cwd", execution_workspace), absolute=True)
            if cwd is not None:
                context["cwd"] = cwd
            pins = command.get("pinned_files")
            inputs = sorted({path for pin in pins if isinstance(pin, dict)
                and pin.get("scope") == "verifier_only"
                and (path := _exact_presentation_path(pin.get("path"), absolute=True)) is not None
            }) if isinstance(pins, list) else []
            if inputs:
                context["verifier_only_inputs"] = inputs
                context["verifier_only_inputs_grant_source_read"] = False
            if len(context) > 1:
                contexts.append(context)
        if contexts:
            projection["focused_verifier"]["execution_context"] = contexts
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
            digest_guidance = (
                'For admitted_source_digest_paths use expected_sha256_ref:"admitted", '
                "not expected_sha256; runtime resolves and checks the exact read-only pin. "
                "For other paths, expected_sha256: current verbatim 64-hex SHA; omit iff unknown. "
                if projection.get("admitted_source_digest_paths") else
                "expected_sha256: this path's current verbatim 64-hex SHA; omit iff unknown. "
            )
            text += ("\nCapsule: evidence, not a read grant. Empty declared_targets or "
                     "evidence_refs do not revoke scopes. "
                     "source_read: bounded exact scoped JSON path/lines/search_terms. "
                     "Batch independent reads; no speculative dependencies/whole-file dumps. "
                     + digest_guidance +
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
        if not verifier_allowed and projection.get("admitted_source_digest_paths"):
            text = text.replace(
                "First read: omit expected_sha256 only if no current exact-path SHA is known. ",
                'For admitted_source_digest_paths use expected_sha256_ref:"admitted"; '
                "the runtime checks this request's readonly capsule pin against the read buffer. "
                "Omit expected_sha256 when using the reference; explicit wrong hashes still reject. ")
        if projection.get("observed_source_digest_reference") is True:
            text = text.replace(
                "Page same file: expected_sha256=returned SHA, start_line=next_line. ",
                'Page same file after a successful read: expected_sha256_ref:"observed", '
                "start_line=next_line; omit expected_sha256. ")
            text += ('For another range/search/JSON selection of a file already successfully '
                     'read by this worker, use expected_sha256_ref:"observed" instead of copying '
                     "the SHA. The runtime retains the first observed version and checks the new "
                     "read buffer; this grants no path and does not refresh changed content. "
                     "Use a literal current SHA for explicit post-edit reads; do not use observed "
                     "before the first successful read.\n")
        if "known_source_sha256_by_path" in projection:
            text += ("Use known_source_sha256_by_path only for the matching path's "
                     "pre-edit expected_sha256.\n")
    if directory_routes:
        text += ("\nread_commands: bash listing_example; shell-quote pinned cat_argv_prefix "
                 "plus a discovered path. ")
        generation = jspace.get("dcf_generation")
        if "directory_source_read" in projection:
            text += ("For a discovered file use command_run command_type=source_read, "
                     "command_line=JSON [path,start_line,end_line]. Only the same "
                     "pinned-cat file/root authority applies, not other exact scopes. "
                     "Resume with [path,next_line,end_line,returned source_sha256]; "
                     "changed content must not be combined with an earlier page. ")
        elif (read_allowed and jspace.get("schema_version") == "jspace_contract_v2"
                and isinstance(generation, dict)
                and generation.get("context_mode") == "local_workspace_jspace"
                and generation.get("dcf_available") is False):
            text += ("Use ranged source_read on discovered regular files under these roots; "
                     "runtime pinned cat/file/root checks must pass. Resume with "
                     "expected_sha256=returned SHA, start_line=next_line. ")
        else:
            text += ("source_read requires source_read:true and an exact "
                     "read_scopes path; directory scopes never qualify. ")
        text += ("No guessed paths, hidden "
                 "files, symlinks, traversal, pipelines, substitution, --pre or --follow; "
                 "runtime pins/limits apply. No new read/write authority.\n")
    if verifier_allowed:
        text += ("\nadmitted verifier_index only; "
                 "caller-bound, no new authority. Verifier-only step group; "
                 "whole-array task limits prevail. No task rewrites/invented capabilities. "
                 "Reuse caller-verified current pre-edit baseline; reproduce if required; "
                 "run required focused post-edit verification. "
                 "Cover required interactions before patching; tests are not the specification. "
                 "Once covered and verified, no extra review round; "
                 "no redundant tests/reads/status polls. "
                 "Reverify after relevant source/test/config changes, failure, "
                 "unknown/mismatched evidence or explicit task requirements. "
                 "Reuse evidence, never ownership/permission clearance. "
                 "Mandatory status updates and terminal fences. "
                 "No argv/cwd/timeout overrides or blocked/cancel bypass.\n"
                 # Measured I23 Ultra worker: two corrective verifier rounds came from these facts,
                 # which coordinators relay inconsistently; state them where tests are written.
                 "Verifier sandbox: admitted cwd; writes only its scratch, TMPDIR. Added tests "
                 "use tempfile/TMPDIR and resolve fixture/helper/data paths from their own file's "
                 "directory, never cwd.\n")
        if read_write_allowed:
            text += ("Optional source_postimages: known exit0/reaped/group-empty success only. "
                     "When the task requires review, semantically review complete definitions/bindings + support, or all-text delta "
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
                     "No dependent same-step verification or missing-evidence edits. "
                     "Include task_status done alone at a strictly later final positive step "
                     "in that SAME commands array. Required checks/readbacks and conclusions must be known, "
                     "not result-dependent. Failed/timed-out/unknown results block done; "
                     "interpretation requires a later model turn. In evidence_only delivery, omit final_reply "
                     "when native receipts contain the required result; add only task-required conclusions "
                     "absent from those receipts. Other delivery modes retain their final_reply contract.\n")
    if read_write_allowed:
        text += ("Complete source_delta: readback per apply_patch rules; else source_read. "
                 "Task-required reads prevail.\n")
        if verifier_allowed:
            text += ("If task-required post-edit review remains: "
                     "Mechanical missing readbacks only; mandatory task_status done "
                     "in later positive steps of the same command_run, exact read+write paths only. "
                     "Batch only fully-known mechanical work; result-dependent review needs "
                     "a later model round. No failed/unknown-effect closure; keep publication fences.\n")
        else:
            text += ("Source-read-only final edits: when task instructions permit a mixed response, "
                     "put fully-known admitted patches in earlier positive steps; still-required "
                     "bounded postimage source_read at a strictly later positive step in the "
                     "same command_run. Exact read+write paths only; no preimage SHA after mutation, "
                     "same-step dependent reads, speculative ranges or scope expansion. "
                     "Keep required verification, readback, status updates and terminal fences.\n")
    return _capability_gap_prompt(text)


def _verifier_fd() -> tuple[int, ...]:
    value = os.environ.get("NOKIY_VERIFIER_FD")
    if value is None:
        return ()
    fd = int(value)
    if fd < 3:
        raise ValueError("NOKIY_VERIFIER_FD_INVALID")
    return (fd,)


def verifier_dispatch_blocker(jspace: dict) -> str | None:
    """Public dispatch readiness, not an internal engine/channel admission."""
    if not jspace.get("verifier_commands"):
        return None
    try:
        if current_process_is_sandboxed():
            return "NOKIY_FULL_CORE_VERIFIER_OUTER_SCOPE_REQUIRED"
    except (OSError, ProcessScopeError):
        return "NOKIY_FULL_CORE_NATIVE_SCOPE_UNAVAILABLE"
    return None


def preflight(request: caller.EmbeddedNokiyRequest, *, post_batch_verifier: bool = False) -> dict[str, Any]:
    ready, _, _, jspace = prepare(request, **({"post_batch_verifier": True} if post_batch_verifier else {}))
    if ready["status"] == "READY":
        blocker = verifier_dispatch_blocker(jspace)
        if blocker:
            return {**ready, "status": "BLOCKED", "first_typed_blocker": blocker}
    return ready


def prepare(request: caller.EmbeddedNokiyRequest, *, post_batch_verifier: bool = False):
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
    context, capsule, jspace = caller._verify_context(request,
        **({"post_batch_verifier": True} if post_batch_verifier else {}))
    if post_batch_verifier and (request.authority_effect != "none" or request.execution_profile != "direct"
            or jspace.get("dcf_generation", {}).get("context_mode") != "local_workspace_jspace"):
        raise caller.EmbeddedNokiyError("NOKIY_POST_BATCH_PREPARATION_UNSUPPORTED",
                                       "post-batch preparation requires a read-only local direct request")
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
    ready["admitted_source_digest_reference"] = (
        ready["status"] == "READY" and _supports_admitted_digest(runtime, jspace, capability_env)
    )
    ready["capsule_source_digest_reference"] = (
        ready["status"] == "READY" and _supports_capsule_digest(runtime, capsule, jspace, capability_env)
    )
    ready["observed_source_digest_reference"] = (
        ready["status"] == "READY" and _supports_observed_digest(runtime, jspace, capability_env)
    )
    ready["directory_source_read"] = (
        ready["status"] == "READY" and _supports_directory_source_read(runtime, jspace, capability_env)
    )
    return ready, runtime, capsule, jspace


def _supervisor_environment(request, environment=None) -> dict[str, str]:
    env = dict(os.environ if environment is None else environment)
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


def _environment(request, runtime, state: Path, *, execution_budget: str | None = None,
                 navigation_contract: dict | None = None) -> dict[str, str]:
    from . import source_navigation
    env = _scrub_receipt_binding(os.environ)
    env.pop(source_navigation.ENV, None)
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
    from .inspect_context import FENCE_ENV, PROFILE_ENV, profile_path
    inspect_profile = profile_path(navigation_contract)
    inspect_policy = (navigation_contract or {}).get("inspect_shell") if inspect_profile is not None else None
    if inspect_policy is not None and os.environ.get(FENCE_ENV) == inspect_policy.get("profile_sha256"):
        # The engine runs under this profile; the router binds every shell it starts to it and
        # J-Space rechecks its digest. Without the applied fence, inspect shells fail closed.
        env[PROFILE_ENV] = str(inspect_profile)
    navigation = source_navigation.build(request.workspace, navigation_contract)
    if navigation is not None:
        env[source_navigation.ENV] = navigation
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


class _EvidenceHandoff:
    """Spool accepted task-status text; authenticate it in existing evidence passes.

    This is optional model content, never an execution observation or acceptance.
    Only bounded text, source identity and a digest survive the current JSONL event.
    """

    def __init__(self, request, run: Path, *, persist=False, readback=False):
        self.request = (request.to_wire() if isinstance(request, caller.EmbeddedNokiyRequest)
                        else request or {})
        self.enabled = self.request.get("terminal_delivery") == "evidence_only"
        self.path, self.persist = run / "worker-handoff.txt", persist
        self.readback = readback
        self.runtime, self.count = None, 0
        self.source, self.reference, self.reason = None, None, "HANDOFF_MISSING"
        self.reply = None

    @staticmethod
    def unavailable(reason):
        return {"result_artifact": None, "result_artifact_source": {
            "schema_version": "nokiy_result_artifact_source_v1", "kind": "worker_final_report",
            "status": "UNAVAILABLE", "reason": reason, "parent_acceptance_required": True}}

    def observe(self, event, index):
        item = event.get("item") if event.get("type") == "item.completed" else None
        if (not self.enabled or not isinstance(item, dict)
                or item.get("type") != "command_execution" or item.get("command_type") != "task_status"):
            return
        runtime = item.get("runtime_id")
        self.runtime = runtime
        self.source, self.reference, self.reason = None, None, "HANDOFF_MISSING"
        self.reply = None
        try:
            output = _structured_output(item)
            control = output.get("task_status")
            if not isinstance(control, dict) or set(output) != {"task_status"}:
                self.count += 1
                raise ValueError("ambiguous task status")
            if control.get("status") != "done":
                if "final_reply" in control:
                    raise ValueError("unaccepted report")
                return
            self.count += 1
            if self.count != 1:
                self.reason = "HANDOFF_AMBIGUOUS"
                return
            from .result_inspection import _native_task_status, _own_edit_observation_settled
            def identity(value):
                return (isinstance(value, str) and 0 < len(value) <= 256 and value.isascii()
                        and all(32 < ord(char) < 127 for char in value))
            provider, step, position = (item.get(key) for key in ("provider_tool_call_id", "step", "command_index"))
            session = "full-" + self.request["request_sha256"]
            command_run = str(runtime) + ".tool.command_run"
            command_id = f"{command_run}:{provider}:{position}"
            if (not all(identity(value) for value in (runtime, provider, item.get("id"), command_id))
                    or item.get("command_run_id") != command_run or item.get("command_id") != command_id
                    or type(step) is not int or not 0 < step < 2**64
                    or type(position) is not int or not 0 <= position < 512
                    or item.get("status") != "completed"
                    or not _own_edit_observation_settled(item, False)
                    or any(key in item and item[key] != expected for key, expected in (
                        ("session_id", session), ("request_id", self.request["request_id"]),
                        ("request_sha256", self.request["request_sha256"]),
                        ("native_thread_id", self.request.get("native_thread_id"))))):
                self.reason = "HANDOFF_IDENTITY_INVALID"
                return
            native_reply = item.get("command") not in ("", "task_status")
            if native_reply:
                self.reason = "HANDOFF_IDENTITY_INVALID"
                if not self.readback or not _native_task_status(event, {command_id: 1}):
                    return
            text = control.get("final_reply")
            if text is None:
                return
            format_name = control.get("final_reply_format", "text")
            if (not isinstance(text, str) or not text.strip() or format_name not in ("text", "json")
                    or "compact_context" in control):
                raise ValueError("invalid report content")
            if format_name == "json" and not native_reply:
                decoded = json.loads(text, object_pairs_hook=caller._unique_object,
                                     parse_constant=caller._invalid_constant)
                if not isinstance(decoded, (dict, list)) or not decoded:
                    raise ValueError("invalid structured report")
                del decoded
            reference = _message_reference(self.path, text)  # Strict, complete UTF-8, without retaining text.
            source = {key: self.request[key] for key in ("request_id", "request_sha256")}
            source.update(session_id=session, runtime_id=runtime, command_id=command_id,
                          provider_tool_call_id=provider, command_index=position, step=step,
                          item_id=item["id"], event_index=index, event_sha256=caller._canonical_sha256(event),
                          final_reply_format=format_name)
            if native_reply:
                # The native JSON command is not a legacy artifact handoff. Expose
                # only its observed content; retain the original unavailable reason.
                preview, truncated = _text_preview(text, self.request["max_result_bytes"])
                self.reply = {
                    "schema_version": "nokiy_task_status_final_reply_v1", "status": "MODEL_REPORTED",
                    "binding": {**source, "native_thread_id": self.request.get("native_thread_id")},
                    "output_json_pointer": "/task_status/final_reply",
                    "content_sha256": reference["sha256"], "content_bytes": reference["bytes"],
                    "text": preview, "complete": not truncated, "truncated": truncated,
                    "truncation_reason": "MAX_RESULT_BYTES" if truncated else None,
                    "parent_acceptance_required": True}
                return
            if self.persist:
                try:
                    with self.path.open("xb") as output:
                        for raw in _text_chunks(text):
                            output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                        if _artifact_stamp(os.fstat(output.fileno())) != _artifact_stamp(self.path.lstat()):
                            raise OSError("report artifact replaced")
                except OSError:
                    self.reason = "HANDOFF_ARTIFACT_UNAVAILABLE"
                    return
            self.source, self.reference = source, reference
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, caller.EmbeddedNokiyError):
            self.count = max(1, self.count)
            self.reason = "HANDOFF_CONTENT_INVALID"

    def events(self, events, delivery):
        for index, event in enumerate(events):
            delivery.observe(event)
            self.observe(event, index)
            yield event

    def reply_fields(self, value, marker, trajectory, command_ids):
        """Readback from the checked trajectory, never a replacement artifact store."""
        if self.reply is None or marker is None or self.count != 1:
            return {}
        source = self.reply["binding"]
        if (self.request.get("request_id") != "tura_embedded_" + str(self.request.get("request_sha256"))
                or self.path.parent.name != self.request.get("request_id")
                or source["session_id"] != marker["session_id"] or source["runtime_id"] != marker["runtime_id"]
                or command_ids.get(source["command_id"]) != 1
                or value.get("requested_terminal_delivery") != "evidence_only"
                or value.get("observed_terminal_delivery") != "evidence_only"
                or caller._canonical_bytes(value.get("terminal_evidence")) != caller._canonical_bytes(marker)):
            return {}
        reference = _artifact_reference(self.path.with_name("core.jsonl"), trajectory)
        return {"task_status_final_reply": {**self.reply, "source_artifact": reference}}

    def fields(self, marker, *, verify=True):
        if marker is None:
            return {}
        if (self.request.get("request_id") != "tura_embedded_" + str(self.request.get("request_sha256"))
                or self.path.parent.name != self.request.get("request_id")
                or marker["session_id"] != "full-" + str(self.request.get("request_sha256"))):
            return self.unavailable("HANDOFF_IDENTITY_INVALID")
        if self.runtime is not None and self.runtime != marker["runtime_id"]:
            return self.unavailable("HANDOFF_FOREIGN_RUNTIME")
        if self.source is None or self.reference is None:
            return self.unavailable(self.reason)
        if verify:
            try:
                _record_artifact(self.path, reference=self.reference)
            except (OSError, caller.EmbeddedNokiyError):
                return self.unavailable("HANDOFF_ARTIFACT_UNAVAILABLE")
        return {"result_artifact": self.reference, "result_artifact_source": {
            "schema_version": "nokiy_result_artifact_source_v1", "kind": "worker_final_report",
            "status": "MODEL_REPORTED", "binding": self.source, "parent_acceptance_required": True}}

    def project(self, value, marker, *, inline_limit: int | None = None):
        # Old receipts remain read-only; an absent optional report never requires replay.
        if "result_artifact_source" not in value and value.get("result_artifact") is None:
            return {}
        expected = self.fields(marker, verify=False)
        try:
            matches = (expected and caller._canonical_bytes(value.get("terminal_evidence")) == caller._canonical_bytes(marker)
                       and value.get("requested_terminal_delivery") == "evidence_only"
                       and value.get("observed_terminal_delivery") == "evidence_only"
                       and caller._canonical_bytes(expected) == caller._canonical_bytes({key: value.get(key) for key in expected}))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            matches = False
        if not matches:
            return self.unavailable("HANDOFF_BINDING_MISMATCH")
        reference = expected.get("result_artifact")
        if inline_limit is None or reference is None or reference["bytes"] > inline_limit:
            return self.fields(marker)
        # Reuse the artifact verification pass, retaining only a complete small
        # body. No preview survives until descriptor, bytes and digest checks pass.
        try:
            with _ArtifactReader(self.path, limit=inline_limit, reference=reference) as artifact:
                raw = b"".join(artifact.chunks())
            text = raw.decode("utf-8")
        except (OSError, UnicodeError, caller.EmbeddedNokiyError):
            return self.unavailable("HANDOFF_ARTIFACT_UNAVAILABLE")
        return {**expected, "result_artifact_preview": {
            "schema_version": "nokiy_result_artifact_preview_v1", "kind": "worker_final_report",
            "status": "MODEL_REPORTED", "encoding": "utf-8", "complete": True,
            "artifact": reference, "text": text, "parent_acceptance_required": True}}


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


_FAILED_TRANSPORT_ERROR = (
    "manas processing failed: network error: provider transport failure: "
    "phase=responses-sse category=decode cause=unexpected-eof"
)
_FAILED_TRANSPORT = {"phase": "responses-sse", "category": "decode", "cause": "unexpected-eof"}
_PREFIX_KEYS = {"schema_version", "session_id", "status", "from_sequence",
                "next_sequence", "terminal_boundary_verified"}


def _valid_failure_prefix(value, session_id):
    return (type(value) is dict and set(value) == _PREFIX_KEYS
            and value["schema_version"] == "execution_evidence_v1"
            and value["session_id"] == session_id and value["status"] == "persisted_prefix"
            and type(value["from_sequence"]) is int and value["from_sequence"] == 0
            and type(value["next_sequence"]) is int and 0 <= value["next_sequence"] <= 2**63 - 1
            and value["terminal_boundary_verified"] is False)


def valid_provider_failure_diagnostic(value, terminal):
    """Validate metadata only; never grant execution or complete failed effects."""
    keys = {"schema_version", "event_index", "trajectory_artifact", "transport",
            "execution_evidence", "model_request_started", "effect_outcome", "parent_acceptance_required"}
    request_id, digest = terminal.get("request_id"), terminal.get("request_sha256")
    reference = value.get("trajectory_artifact") if type(value) is dict else None
    if (type(value) is not dict or set(value) not in (keys, keys | {"stream_failure_snapshot"})
            or value["schema_version"] != "nokiy_provider_failure_diagnostic_v1"
            or terminal.get("status") != "BLOCKED" or terminal.get("cleanup_pass") is not True
            or not isinstance(terminal.get("first_typed_blocker"), str) or not terminal["first_typed_blocker"]
            or not isinstance(request_id, str) or not caller.REQUEST_ID_PATTERN.fullmatch(request_id)
            or not isinstance(digest, str)
            or request_id != "tura_embedded_" + str(digest)
            or type(value["event_index"]) is not int or not 0 <= value["event_index"] <= 2**63 - 1
            or type(value["transport"]) is not dict or value["transport"] != _FAILED_TRANSPORT
            or not _valid_failure_prefix(value["execution_evidence"], "full-" + str(digest))
            or value["model_request_started"] != "UNKNOWN" or value["effect_outcome"] != "UNKNOWN"
            or value["parent_acceptance_required"] is not True
            or type(reference) is not dict or set(reference) != {"path", "sha256", "bytes"}
            or reference != terminal.get("trajectory_artifact")
            or not isinstance(reference["path"], str) or not Path(reference["path"]).is_absolute()
            or any(ord(char) < 32 for char in reference["path"])
            or Path(reference["path"]).name != "core.jsonl" or Path(reference["path"]).parent.name != request_id
            or not isinstance(reference["sha256"], str) or len(reference["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in reference["sha256"])
            or type(reference["bytes"]) is not int or not 0 <= reference["bytes"] <= 2**63 - 1):
        return None
    if "stream_failure_snapshot" in value and not _valid_stream_failure_evidence(
            value["stream_failure_snapshot"], value["execution_evidence"]["session_id"]):
        return None
    return {**value, "trajectory_artifact": dict(reference), "transport": dict(_FAILED_TRANSPORT),
            "execution_evidence": dict(value["execution_evidence"])}


def _valid_stream_failure_evidence(value, session):
    keys = {"snapshot", "session_id", "output_event_seq", "failure_event_seq", "last_event_seq",
            "index_artifact", "database_artifact"}
    if type(value) is not dict or set(value) != keys or value["session_id"] != session:
        return False
    integer = lambda n: type(n) is int and 0 <= n <= 2**63 - 1
    if (not all(integer(value[k]) for k in ("output_event_seq", "failure_event_seq", "last_event_seq"))
            or not value["output_event_seq"] < value["failure_event_seq"] <= value["last_event_seq"]):
        return False
    snapshot = value["snapshot"]
    fields = {"schema_version", "event_count", "completed_seen", "completion_valid", "completed_frame_ended",
              "pending_bytes", "request_id", "runtime_id", "command_effect_started"}
    if (type(snapshot) is not dict or set(snapshot) != fields
            or snapshot["schema_version"] != "provider_stream_failure_snapshot_v1"
            or not all(integer(snapshot[k]) for k in ("event_count", "pending_bytes"))
            or not all(type(snapshot[k]) is bool for k in
                       ("completed_seen", "completion_valid", "completed_frame_ended", "command_effect_started"))
            or (snapshot["completion_valid"] or snapshot["completed_frame_ended"]) and not snapshot["completed_seen"]
            or snapshot["completed_seen"] and snapshot["event_count"] == 0):
        return False
    runtime = snapshot["runtime_id"]
    request = snapshot["request_id"]
    if (not isinstance(runtime, str) or not runtime.startswith("runtime-") or not 8 <= len(runtime) <= 128
            or not runtime.isascii() or any(not (c.isalnum() or c in "-_") for c in runtime)
            or request is not None and (not isinstance(request, str) or not 1 <= len(request) <= 256
                                       or not request.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in request))):
        return False
    for key in ("index_artifact", "database_artifact"):
        ref = value[key]
        if (type(ref) is not dict or set(ref) != {"path", "sha256", "bytes"}
                or not isinstance(ref["path"], str) or not isinstance(ref["sha256"], str)
                or len(ref["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in ref["sha256"])
                or not integer(ref["bytes"]) or ref["bytes"] > 64 * 1024 * 1024):
            return False
    parts = Path(value["database_artifact"]["path"]).parts
    return (value["index_artifact"]["path"] == "execution-state/session_log/index.sqlite3"
            and len(parts) == 6 and parts[:3] == ("execution-state", "session_log", "workspaces")
            and len(parts[3]) == 64 and all(c in "0123456789abcdef" for c in parts[3])
            and parts[4:] == (".tura", "session_log.sqlite3"))


def provider_failure_diagnostic_with_snapshot(value, terminal, run, *, require_existing=False):
    """Supplement a proved failed prefix; optional evidence never replaces its blocker."""
    value = valid_provider_failure_diagnostic(value, terminal)
    existing = terminal.get("provider_failure_diagnostic")
    if value is None or require_existing and (type(existing) is not dict or "stream_failure_snapshot" not in existing):
        return value
    base = {k: v for k, v in value.items() if k != "stream_failure_snapshot"}
    scope = terminal.get("cleanup")
    if (not isinstance(scope, dict) or scope.get("engine_reaped") is not True
            or scope.get("no_live_descendants") is not True or scope.get("cleanup_error") is not None):
        return base
    from . import durable_recovery, result_inspection
    try:
        evidence = durable_recovery.provider_stream_failure_evidence(
            run, base["execution_evidence"], result_inspection)
        candidate = {**base, "stream_failure_snapshot": evidence}
        return valid_provider_failure_diagnostic(candidate, terminal) or base
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return base


class _FailedTurnDiagnostic:
    """Accumulate one failed terminal during the existing complete evidence pass."""

    def __init__(self, request):
        self.expected = _terminal_identity(request)
        self.summary = _TurnSummary(self.expected, require_completed=False)
        self.valid, self.complete, self.candidate = True, False, None

    def events(self, events):
        for index, event in enumerate(events):
            if event.get("type") == "turn.completed" and self.valid:
                try:
                    self.summary.observe(event)
                except (ValueError, TypeError, caller.EmbeddedNokiyError):
                    self.valid = False
                if (self.valid and event.get("status") == "failed"
                        and event.get("error") == _FAILED_TRANSPORT_ERROR
                        and _valid_failure_prefix(event.get("execution_evidence"), self.expected["session_id"])):
                    self.candidate = {"event_index": index,
                                      "execution_evidence": dict(event["execution_evidence"])}
            yield event
        self.complete = True

    def finish(self, reference):
        if not self.valid or not self.complete or self.candidate is None:
            return None
        try:
            self.summary.finish()
        except caller.EmbeddedNokiyError:
            return None
        return {"schema_version": "nokiy_provider_failure_diagnostic_v1", **self.candidate,
                "trajectory_artifact": reference, "transport": dict(_FAILED_TRANSPORT),
                "model_request_started": "UNKNOWN", "effect_outcome": "UNKNOWN",
                "parent_acceptance_required": True}


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
        handoff = _EvidenceHandoff(request, path.parent, persist=True)
        def observations():
            nonlocal preview, truncated, result_ref, tools, successful, output
            for event in handoff.events(items, delivery):
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
            "command_evidence": evidence, "turn_completed": True, **delivery.fields(),
            **handoff.fields(delivery.marker)}


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


class _ServiceStartupError(caller.EmbeddedNokiyError):
    def __init__(self, service: str, reason: str, *, exit_code=None, os_errno=None):
        super().__init__("NOKIY_FULL_CORE_SERVICE_NOT_READY", service)
        self.diagnostic = {"schema_version": "nokiy_full_core_startup_failure_v1",
                           "service": service, "reason": reason, "exit_code": exit_code,
                           "os_errno": os_errno, "log_artifact": None, "log_tail": None,
                           "log_truncated": None, "log_capture_blocker": None}


def _startup_log(run: Path, service: str, *, reference=None) -> dict[str, Any]:
    if service not in {"tura_session_db", "tura_router"}:
        raise caller.EmbeddedNokiyError("STARTUP_DIAGNOSTIC_INVALID", "unknown service")
    tail = b""
    with _ArtifactReader(run / (service + ".log"), limit=4 * 1024 * 1024,
                         reference=reference) as source:
        for raw in source.chunks():
            tail = (tail + raw)[-4096:]
    text, truncated = _text_preview(tail.decode("utf-8", errors="replace"), 4096)
    return {"log_artifact": source.record, "log_tail": text,
            "log_truncated": source.size > len(tail) or truncated}


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
        ready, runtime, _, jspace = prepare(request)
        if ready["status"] != "READY":
            raise caller.EmbeddedNokiyError(ready["first_typed_blocker"],
                                            "required router capability unavailable")
        timing.observe("prepare")
        state = run / "execution-state"
        state.mkdir(mode=0o700)
        env = _environment(request, runtime, state, execution_budget=os.environ.get(BUDGET_ENV),
                           navigation_contract=jspace)
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
            try:
                process = subprocess.Popen([str(runtime.artifacts[name].path), *arguments],
                                           cwd=request.workspace, env=service_env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, pass_fds=fds)
            except OSError as error:
                raise _ServiceStartupError(name, "spawn_failed", os_errno=error.errno) from error
            finally:
                for fd in fds:
                    os.close(fd)
            processes.append(process)
            deadline = time.monotonic() + (15 if request.timeout_seconds is None else min(15, request.timeout_seconds))
            endpoint_path = state / "session_log" / marker
            while not endpoint_path.is_file():
                exit_code = process.poll()
                if exit_code is not None or time.monotonic() > deadline:
                    raise _ServiceStartupError(name, "process_exited" if exit_code is not None
                                               else "readiness_timeout", exit_code=exit_code)
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
        if isinstance(original_error, _ServiceStartupError):
            # Read only after cleanup has stopped the writer. Diagnostics never
            # replace a startup refusal or retry it in a different directory.
            try:
                original_error.diagnostic.update(_startup_log(run, original_error.diagnostic["service"]))
            except caller.EmbeddedNokiyError as error:
                original_error.diagnostic["log_capture_blocker"] = error.code
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
    try:
        root_parent = inherited_root_parent()
    except (OSError, ProcessScopeError) as error:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_NATIVE_SCOPE_UNAVAILABLE", str(error)) from error
    ready, runtime, capsule, jspace = prepare(request)
    if ready["status"] != "READY":
        raise caller.EmbeddedNokiyError(ready["first_typed_blocker"],
                                      "scoped verifier cannot start inside the inherited process fence")
    if root_parent is not None and jspace.get("verifier_commands"):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_VERIFIER_OUTER_SCOPE_REQUIRED",
                                      "focused verifiers still require an external parent scope")
    try:
        run.mkdir(mode=0o700)
    except FileExistsError as error:
        raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_UNCERTAIN_PRIOR_ATTEMPT", "run exists without terminal") from error
    # Held until the terminal is sealed, so read-result waits for this live run instead of
    # reporting it as interrupted; the kernel releases the lock if this process dies.
    with caller.execution_lock(run):
        return _execute_created_run(request, run, root_parent, ready, runtime, capsule, jspace)


def _execute_created_run(request: caller.EmbeddedNokiyRequest, run: Path, root_parent, ready: dict[str, Any],
                         runtime, capsule: dict[str, Any], jspace: dict[str, Any]) -> dict[str, Any]:
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
                                     terminal_delivery=request.terminal_delivery,
                                     workspace=str(request.workspace),
                                     admitted_digest_reference=ready.get(
                                         "admitted_source_digest_reference") is True,
                                     observed_digest_reference=ready.get(
                                         "observed_source_digest_reference") is True,
                                     directory_source_read=ready.get("directory_source_read") is True,
                                     capsule=capsule,
                                     capsule_digest_reference=ready.get(
                                         "capsule_source_digest_reference") is True).encode())
    # Add a signal-only fence; do not relax inherited filesystem/network policy.
    # Actual tool writes remain under the full core's sandbox and J-Space contract.
    fence = "(version 1) (allow default) (deny signal) (allow signal (target same-sandbox))"
    command = ["/usr/bin/sandbox-exec", "-p", fence, sys.executable, "-B", "-m",
               MODULE, "--supervise", str(run / "execution.json"), "--parent-pid", str(os.getpid())]
    from .inspect_context import FENCE_ENV, profile_path, tool_env as inspect_tool_env
    inspect_profile = profile_path(jspace)
    if inspect_profile is not None:
        # Inspect runs replace the signal-only fence with the bound write-confined profile,
        # which keeps the same signal rules; every engine process and worker shell inherits it.
        command[1:3] = ["-f", str(inspect_profile)]
    trajectory, errors = run / "supervision.json", run / "supervision.stderr"
    engine, scope = {}, {}
    supervision_reference = trajectory_reference = None
    failure = None
    wall = 0.0
    code = -1
    channel = verifier = parent_shell = None
    env = _scrub_receipt_binding(os.environ)
    env.pop("NOKIY_VERIFIER_FD", None)
    kwargs = {}
    try:
        if jspace.get("verifier_commands") or jspace.get("parent_shell"):
            from .verifier_channel import VerifierChannel
            deadline = None if request.timeout_seconds is None else time.monotonic() + request.timeout_seconds
            if jspace.get("verifier_commands"):
                from .verifier_parent import ParentVerifier
                # prepare/_verify_context already checked authority/content digests
                # and live DCF freshness for precisely this action-scoped generation.
                generation = capsule.get("dcf_generation", {})
                validated_dcf_generation = (generation
                    if jspace.get("schema_version") == "jspace_contract_v2"
                    and ready.get("context", {}).get("context_mode") == "dcf_jspace_required"
                    and "action_freshness" in generation else None)
                verifier = ParentVerifier(request.workspace, jspace, runtime.artifacts["tura_router"],
                                          deadline, validated_dcf_generation=validated_dcf_generation,
                                          diagnostic_root=run, request_id=request.request_id,
                                          request_sha256=request.request_sha256)
            if jspace.get("parent_shell"):
                # Shell lines of a writing action run here, under the profile the contract binds.
                from .parent_shell import ParentShell
                parent_shell = ParentShell(request.workspace, jspace, request.artifact_root)
            # Transport/handler limits are per operation, never an idle lifetime.
            channel_timeout = (max([grant["timeout_seconds"] for grant in jspace.get("verifier_commands") or []]
                                   or [300])
                               if request.timeout_seconds is None else request.timeout_seconds) + 20
            channel = VerifierChannel(jspace["authorization_semantic_sha256"],
                                      len(jspace.get("verifier_commands") or []),
                                      verifier.handle_request if verifier is not None else None,
                                      timeout_seconds=channel_timeout,
                                      shell_handler=parent_shell.handle_request if parent_shell else None,
                                      ).__enter__()
            env["NOKIY_VERIFIER_FD"] = str(channel.router_fd)
            kwargs = {"pass_fds":(channel.router_fd,), "on_spawn":channel.release_router_copy}
        if root_parent is None:
            with ExitStack() as fence_services:
                if inspect_profile is not None:
                    env[FENCE_ENV] = jspace["inspect_shell"]["profile_sha256"]
                    env["TMPDIR"] = str(inspect_profile.parent / "scratch")
                    env["TMPPREFIX"] = str(inspect_profile.parent / "scratch" / "zsh")
                    env.update(inspect_tool_env(inspect_profile.parent))
                    # Seatbelt refuses setuid /bin/ps; serve ps from outside the fence for this run.
                    from .process_broker import ProcessBroker, shell_env
                    try:
                        env.update(shell_env(inspect_profile.parent, sys.executable, env.get("PATH", "")))
                    except OSError:
                        pass
                    fence_services.enter_context(ProcessBroker.for_root(inspect_profile.parent))
                code, wall, failure, _ = caller._run_process(command, cwd=request.workspace,
                    env=env, stdin_path=run / "prompt.txt", stdout_path=trajectory,
                    stderr_path=errors, timeout=None if request.timeout_seconds is None else request.timeout_seconds + 30,
                    output_limit=MAX_SUPERVISION_BYTES, **kwargs)
        else:
            # The caller already owns the native command's kernel signal scope.
            # Stay at that root; a child supervisor would also own this caller.
            started = time.monotonic()
            with caller._cancellation_signals() as cancelled, trajectory.open("xb") as output, errors.open("xb") as stderr:
                if cancelled:
                    raise caller.EmbeddedNokiyError("NOKIY_EMBEDDED_CANCELLED", "cancelled before spawn")
                supervised, summary = _supervise_request(request, run / "execution.json", root_parent,
                    SimpleNamespace(is_set=lambda: bool(cancelled)), environment=env)
                summary["scope"]["isolation_source"] = "native_command_root"
                output.write((_summary_json(summary, MAX_SUPERVISION_BYTES) + "\n").encode())
                stderr.write(supervised.stderr)
                failure = "NOKIY_EMBEDDED_CANCELLED" if cancelled else None
                code = 2 if failure or summary["failure"] else 0
            wall = time.monotonic() - started
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
        if parent_shell is not None:
            parent_shell.close()
    cleanup = scope.get("engine_reaped") is True and scope.get("no_live_descendants") is True and not scope.get("cleanup_error")
    if verifier is not None:
        scope["parent_verifier"] = {"calls":verifier.calls, "cleanup_pass":verifier.cleanup_pass,
                                    "channel_failure":(channel.failure or "")[:2048] if channel else None}
        cleanup = cleanup and verifier.cleanup_pass
    if parent_shell is not None:
        scope["parent_shell"] = {"calls": parent_shell.calls,
                                 "channel_failure": (channel.failure or "")[:2048] if channel else None}
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
    delivery = _TerminalEvidence(request)
    delivery_failure = None
    try:
        if engine.get("turn_completed") is True:
            if engine.get("terminal_evidence") is not None:
                delivery.observe(engine["terminal_evidence"])
            delivery.validate_projection(engine)
    except caller.EmbeddedNokiyError as error:
        delivery_failure = error.code
        delivery = _TerminalEvidence(request)
    preview, truncated, result_artifact = "", False, None
    try:
        preview, truncated, result_artifact = _result_projection(
            run, engine.get("final_text", ""), engine.get("result_truncated", False),
            None if delivery.marker is not None else engine.get("result_artifact"), request.max_result_bytes)
    except (OSError, UnicodeError, caller.EmbeddedNokiyError) as error:
        failure = failure or getattr(error, "code", "NOKIY_FULL_CORE_RESULT_INVALID")
    if not preview:
        failure = failure or "NOKIY_EMBEDDED_RESULT_MISSING"
    failure = failure or delivery_failure
    evidence_text = delivery.result_text()
    if evidence_text is not None and (preview != evidence_text or truncated):
        failure = failure or "NOKIY_FULL_CORE_RESULT_INVALID"
    handoff_fields = (_EvidenceHandoff.unavailable("HANDOFF_EVIDENCE_UNAVAILABLE")
                      if delivery.marker is not None and "result_artifact_source" in engine else {})
    command_evidence = engine.get("command_evidence")
    evidence_artifact = None
    if isinstance(command_evidence, dict):
        evidence_path = run / "command-evidence.json"
        caller._write_create_only(evidence_path, command_evidence)
        evidence_artifact = caller._record(evidence_path)
    file_evidence = None
    file_artifact = None
    failure_diagnostic = None
    core_path = run / "core.jsonl"
    if core_path.exists() or core_path.is_symlink() or engine.get("turn_completed") is True:
        try:
            expected_core = (_artifact_reference(core_path, engine.get("trajectory_artifact"))
                             if engine.get("turn_completed") is True else None)
            trajectory_reference = _record_artifact(core_path, reference=expected_core)
            events = _Trajectory(core_path, request.max_trajectory_bytes, reference=trajectory_reference)
            diagnostic = _FailedTurnDiagnostic(request) if failure is not None else None
            observed_events = diagnostic.events(events) if diagnostic is not None else events
            correlated_delivery = _TerminalEvidence(request)
            handoff = _EvidenceHandoff(request, run)
            file_evidence = file_change_evidence.produce(
                handoff.events(observed_events, correlated_delivery), request, jspace)
            if engine.get("turn_completed") is True:
                correlated_delivery.validate_projection(engine)
                if correlated_delivery.marker is not None:
                    handoff_fields = handoff.project(engine, correlated_delivery.marker)
            if diagnostic is not None:
                failure_diagnostic = diagnostic.finish(trajectory_reference)
            if file_evidence is not None:
                file_path = run / "file-change-evidence.json"
                caller._write_create_only(file_path, file_evidence)
                file_artifact = caller._record(file_path)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError,
                caller.EmbeddedNokiyError) as error:
            failure_diagnostic = None
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
            failure_diagnostic = None
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
        **handoff_fields,
    }
    failure_diagnostic = valid_provider_failure_diagnostic(failure_diagnostic, receipt)
    if isinstance(engine.get("startup_failure_diagnostic"), dict):
        receipt["startup_failure_diagnostic"] = engine["startup_failure_diagnostic"]
    if failure_diagnostic is not None:
        failure_diagnostic = provider_failure_diagnostic_with_snapshot(failure_diagnostic, receipt, run)
        receipt["provider_failure_diagnostic"] = failure_diagnostic
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
    from .session_log_retention import retire
    retire(run, receipt)
    return receipt


def _summary_json(value: dict[str, Any], limit: int) -> str:
    encoded = json.dumps(value).encode("utf-8")
    if len(encoded) + 1 > limit:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_CORE_SUMMARY_LIMIT_EXCEEDED", "supervision summary too large")
    return encoded.decode("utf-8")


def _supervise_request(request, spec, parent_pid, cancelled, *, environment=None, pass_fds=()):
    outcome = supervise([sys.executable, "-B", "-m", MODULE, "--engine", str(spec)],
        cwd=str(request.workspace), env=_supervisor_environment(request, environment),
        input_bytes=b"", timeout=request.timeout_seconds, cancelled=cancelled,
        max_stdout=MAX_ENGINE_SUMMARY_BYTES, max_stderr=65536,
        parent_pid=parent_pid, pass_fds=pass_fds)
    result = json.loads(outcome.stdout, object_pairs_hook=caller._unique_object,
                        parse_constant=caller._invalid_constant) if outcome.stdout else {}
    failure = outcome.failure or result.get("failure")
    if outcome.returncode != 0:
        failure = failure or "NOKIY_FULL_CORE_ENGINE_FAILED"
    return outcome, {"result": result, "scope": outcome.scope, "failure": failure}


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
            result = {"failure": getattr(error, "code", type(error).__name__)}
            if isinstance(error, _ServiceStartupError):
                result["startup_failure_diagnostic"] = error.diagnostic
            print(_summary_json(result, MAX_ENGINE_SUMMARY_BYTES))
            return 2
    spec = caller._load_json(args.supervise, limit=caller.MAX_REQUEST_BYTES, code="NOKIY_FULL_CORE_SPEC_INVALID")
    request = caller.decode_request(spec["request"])
    cancelled = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: cancelled.set())
    try:
        _, summary = _supervise_request(request, args.supervise, args.parent_pid, cancelled,
                                       pass_fds=_verifier_fd())
        print(_summary_json(summary, MAX_SUPERVISION_BYTES))
        return 2 if summary["failure"] else 0
    except Exception as error:
        print(json.dumps({"failure": getattr(error, "code", type(error).__name__), "scope": {}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
