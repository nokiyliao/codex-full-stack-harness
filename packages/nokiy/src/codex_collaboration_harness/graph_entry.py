# SPDX-License-Identifier: MIT
"""Optional same-task tool adapter for the separately licensed Rust graph engine.

No model loop, task database, callback transport, or background scheduler lives
here. The caller retains task ownership and authorization; DCF is a read-only
input and the Rust engine owns its bounded per-call recovery journal.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import importlib
import json
import os
import re
import shlex
import signal
import stat
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .core import canonical_sha256

MAX_INPUT = 524_288
MAX_ENGINE_OUTPUT = 64 * 1024 * 1024
MAX_TASK_BYTES = 128 * 1024 * 1024
MAX_TASK_FILES = 2048
MAX_RESPONSE = 65_536
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class GraphEntryError(ValueError):
    """A refusal before dispatch, not evidence about earlier call effects."""


@dataclass(frozen=True)
class GraphSettings:
    engine: Path
    engine_sha256: str
    dcf_root: Path
    artifact_root: Path
    caller_task_id: str | None = None


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GraphEntryError("GRAPH_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def decode(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_INPUT:
        raise GraphEntryError("GRAPH_ENTRY_INPUT_LIMIT")
    value = json.loads(data, object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise GraphEntryError("GRAPH_ENTRY_OBJECT_REQUIRED")
    return value


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _plain_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise GraphEntryError("GRAPH_EXECUTABLE_IDENTITY_INVALID")


def _digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class DcfBoundary:
    """Reuse the installed repository's compiler and exact freshness reader."""

    def __init__(self, repo_root: Path) -> None:
        self.root = repo_root.resolve(strict=True)
        sys.path.insert(0, str(self.root))
        self.jspace = importlib.import_module("scripts.ops.dcf.jspace")
        runtime_module = importlib.import_module("scripts.ops.dcf.runtime")
        for module in (self.jspace, runtime_module):
            if not Path(module.__file__).resolve().is_relative_to(self.root):
                raise GraphEntryError("GRAPH_DCF_IMPORT_ROOT_MISMATCH")
        self.runtime = runtime_module.DcfRuntime(self.root)

    def verify(self, contract: dict[str, Any]) -> None:
        self.jspace.canonical_contract_bytes(contract)
        self.jspace.verify_contract_freshness(self.runtime, contract)

    def compile(self, surface_id: str, action: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        contract = self.jspace.compile_jspace_contract(self.runtime, surface_id=surface_id, action=action)
        return contract, self.jspace.compile_task_context_capsule(contract, action=action)


def verify_context(
    payload: dict[str, Any], settings: GraphSettings,
    verify_freshness: Callable[[dict[str, Any]], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if set(payload) != {"request", "context"}:
        raise GraphEntryError("GRAPH_ENTRY_FIELDS_INVALID")
    request, context = payload["request"], payload["context"]
    if not isinstance(request, dict) or not isinstance(context, dict):
        raise GraphEntryError("GRAPH_CONTEXT_REQUIRED")
    # Match Rust Request's deny_unknown_fields even when auto never calls Rust.
    required = {"schema_version", "task_id", "call_id", "workspace", "expires_at_unix",
                "timeout_ms", "jspace", "graph"}
    if not required <= request.keys() <= required | {"preconditions"}:
        raise GraphEntryError("GRAPH_REQUEST_FIELDS_INVALID")
    task_id = request.get("task_id")
    if not isinstance(task_id, str) or not _IDENTITY.fullmatch(task_id):
        raise GraphEntryError("GRAPH_TASK_ID_INVALID")
    if (not isinstance(settings.caller_task_id, str)
            or not _IDENTITY.fullmatch(settings.caller_task_id)):
        raise GraphEntryError("GRAPH_CALLER_TASK_BINDING_REQUIRED")
    if task_id != settings.caller_task_id:
        raise GraphEntryError("GRAPH_CALLER_TASK_MISMATCH")
    if context.get("schema_version") != "task_context_capsule_v1":
        raise GraphEntryError("GRAPH_CONTEXT_SCHEMA_INVALID")
    semantic = canonical_sha256({k: v for k, v in context.items() if k != "semantic_sha256"})
    if semantic != context.get("semantic_sha256"):
        raise GraphEntryError("GRAPH_CONTEXT_DIGEST_MISMATCH")
    mission = context.get("mission", {})
    if not isinstance(mission, dict) or mission.get("task_id") != task_id:
        raise GraphEntryError("GRAPH_CONTEXT_TASK_MISMATCH")
    if any(not isinstance(mission.get(k), str) or not mission[k].strip()
           for k in ("mission_id", "mode", "current_predicate", "objective")):
        raise GraphEntryError("GRAPH_CONTEXT_MISSION_INCOMPLETE")
    contract = request.get("jspace")
    if not isinstance(contract, dict) or contract.get("schema_version") != "jspace_contract_v2":
        raise GraphEntryError("GRAPH_JSPACE_V2_REQUIRED")
    root = settings.dcf_root.resolve(strict=True)
    if (request.get("workspace") != str(root) or contract.get("repo_root") != str(root)
            or context.get("surface", {}).get("repo_root") != str(root)):
        raise GraphEntryError("GRAPH_WORKSPACE_BINDING_MISMATCH")
    for key in ("declared_targets", "matched_surface_ids"):
        if context.get("surface", {}).get(key) != contract.get(key):
            raise GraphEntryError("GRAPH_CONTEXT_SCOPE_MISMATCH")
    if context.get("dcf_generation") != contract.get("dcf_generation"):
        raise GraphEntryError("GRAPH_CONTEXT_GENERATION_MISMATCH")
    if context.get("jspace_semantic_sha256") != contract.get("authorization_semantic_sha256"):
        raise GraphEntryError("GRAPH_CONTEXT_JSPACE_MISMATCH")
    if context.get("authority", {}).get("denied_operations") != contract.get("denied_operations"):
        raise GraphEntryError("GRAPH_CONTEXT_AUTHORITY_MISMATCH")
    if context.get("focused_verifiers") != contract.get("focused_verifiers"):
        raise GraphEntryError("GRAPH_CONTEXT_VERIFIER_MISMATCH")
    expiry, timeout = request.get("expires_at_unix"), request.get("timeout_ms")
    if (type(expiry) is not int or not time.time() < expiry <= time.time() + 300
            or type(timeout) is not int or not 1 <= timeout <= 120_000):
        raise GraphEntryError("GRAPH_ENTRY_BUDGET_INVALID")
    if len(_encoded(request)) > 262_144:
        raise GraphEntryError("GRAPH_ENGINE_INPUT_LIMIT")
    verify_freshness(contract)
    return request, context


def _scope_rule(root: Path, scope: str) -> str:
    if not isinstance(scope, str) or not scope:
        raise GraphEntryError("GRAPH_SANDBOX_SCOPE_INVALID")
    subtree = scope.endswith("/**")
    relative = scope[:-3] if subtree else scope
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or any(c in relative for c in "*?[]"):
        raise GraphEntryError("GRAPH_SANDBOX_SCOPE_UNSUPPORTED:use exact paths or trailing /**")
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise GraphEntryError("GRAPH_SANDBOX_SCOPE_ESCAPE")
    return f'({"subpath" if subtree else "literal"} {json.dumps(str(target))})'


def sandbox_profile(request: dict[str, Any], settings: GraphSettings, state: Path) -> str:
    """A local macOS execution boundary, separate from J-Space declarations."""
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise GraphEntryError("GRAPH_OS_SANDBOX_UNAVAILABLE")
    root = settings.dcf_root.resolve()
    contract = request["jspace"]
    read = [_scope_rule(root, s) for s in contract["read_scopes"]]
    write = [_scope_rule(root, s) for s in contract["write_scopes"]]
    # Import/discovery needs directory entries, not sibling file contents.
    parents = set()
    for scope in contract["read_scopes"]:
        if scope.endswith("/**"):
            continue
        parent = (root / Path(scope).parent).resolve()
        if not parent.is_relative_to(root):
            raise GraphEntryError("GRAPH_SANDBOX_SCOPE_ESCAPE")
        if parent != root:
            parents.add(str(parent))
    directory_rules = [
        f'(require-all (literal {json.dumps(parent)}) (vnode-type DIRECTORY))'
        for parent in sorted(parents)
    ]
    # Runtime libraries are readable, not writable. Credentials, browser state,
    # and unrelated user files are outside these grants.
    system = ["/System", "/usr", "/bin", "/sbin", "/Library/Apple",
              str(Path(sys.base_prefix).resolve()), str(Path(sys.prefix).resolve()),
              str(root / ".venv")]
    read.extend(f'(subpath {json.dumps(p)})' for p in system)
    read.extend(['(literal "/")', f'(literal {json.dumps(str(settings.engine.resolve()))})',
                 f'(literal {json.dumps(str(Path(__file__).with_name("graph_process.py").resolve()))})',
                 f'(literal {json.dumps(str(root))})',
                 f'(subpath {json.dumps(str(state))})', '(literal "/dev/urandom")',
                 '(literal "/dev/null")'])
    write.extend([f'(subpath {json.dumps(str(state))})', '(literal "/dev/null")'])
    return "\n".join([
        "(version 1)", "(deny default)", "(allow process-exec process-fork)",
        "(allow signal (target same-sandbox))", "(allow sysctl-read)",
        "(allow file-read-metadata)", "(allow file-read* " + " ".join(read) + ")",
        *(["(allow file-read-data " + " ".join(directory_rules) + ")"]
          if directory_rules else []),
        "(allow file-write* " + " ".join(write) + ")",
        "(deny file-write-unlink " + f'(subpath {json.dumps(str(root))})' + ")",
        "(deny network*)",
    ])


def _artifact_state(settings: GraphSettings, task_id: str) -> Path:
    root = settings.artifact_root.resolve(strict=True)
    if root.is_relative_to(settings.dcf_root.resolve()):
        raise GraphEntryError("GRAPH_ARTIFACT_ROOT_INSIDE_WORKSPACE")
    state = root / "tura-graph" / task_id
    for path in (root / "tura-graph", state):
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise GraphEntryError("GRAPH_ARTIFACT_DIRECTORY_INVALID")
    total = count = 0
    for base, directories, names in os.walk(state, followlinks=False):
        for name in directories + names:
            info = (Path(base) / name).lstat()
            if stat.S_ISLNK(info.st_mode):
                raise GraphEntryError("GRAPH_ARTIFACT_SYMLINK_REJECTED")
            count += 1
            total += info.st_size
            if count > MAX_TASK_FILES or total > MAX_TASK_BYTES:
                raise GraphEntryError("GRAPH_TASK_ARTIFACT_BUDGET:close out existing calls first")
    return state


async def _bounded_read(stream: asyncio.StreamReader | None, maximum: int) -> bytes:
    if stream is None:
        return b""
    result = bytearray()
    while chunk := await stream.read(65536):
        result.extend(chunk)
        if len(result) > maximum:
            raise GraphEntryError("GRAPH_ENGINE_OUTPUT_LIMIT_EFFECT_UNSETTLED")
    return bytes(result)


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
        # The inner supervisor needs its engine TERM grace plus descendant cleanup.
        await asyncio.wait_for(process.wait(), timeout=22)
    except ProcessLookupError:
        return
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def _response_bytes(result: dict[str, Any]) -> bytes:
    # SDK float formatting differs from json.dumps. Measure both actual
    # serializers and reserve a CLI newline, without requiring the optional SDK.
    encoded = (json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    try:
        from pydantic_core import to_json
    except ImportError:
        return encoded
    sdk = to_json(result, fallback=str, indent=2) + b"\n"
    return max(encoded, sdk, key=len)


def bounded_response(result: dict[str, Any]) -> dict[str, Any]:
    """Bound the final projection without ever recomputing engine provenance."""
    if len(_response_bytes(result)) <= MAX_RESPONSE:
        return result
    compact = {key: value for key, value in result.items() if key != "graph"}
    compact["output_truncated"] = True
    graph = result.get("graph")
    if isinstance(graph, dict):
        compact.update(cancel_reason=graph.get("cancel_reason"), cancelled=graph.get("cancelled"),
                       node_summary=[{key: row[key] for key in ("id", "call_id", "success") if key in row}
                                     for row in graph.get("results", []) if isinstance(row, dict)])
    if len(_response_bytes(compact)) <= MAX_RESPONSE:
        return compact
    # A huge error or process diagnostic cannot bypass the final bound. This
    # blocks result intake, not an assertion that the commands did not execute.
    blocked = {
        "status": "blocked", "error": "GRAPH_RESPONSE_BUDGET_EXCEEDED",
        "effect_state": "unproven", "retry_safe": False, "fallback_performed": False,
        "output_truncated": True, "response_payload_sha256": canonical_sha256(result),
    }
    for key in ("task_id", "call_id", "request_digest", "context_semantic_sha256",
                "full_result_sha256", "full_result_bytes", "full_result_representation",
                "recovery_artifact", "recovery_artifact_root", "replayed", "effect_state",
                "retrieval_only", "recorded_status", "execution_started", "source_mutation_count",
                "authority_effect", "task_terminal_verified", "archive_sha256", "guard_sha256"):
        if key in result and len(_response_bytes({key: result[key]})) <= 4096:
            blocked[key] = result[key]
    if isinstance(result.get("status"), str) and len(result["status"]) <= 64:
        blocked["observed_status"] = result["status"]
    return blocked


def compact_result(result: dict[str, Any], state: Path) -> dict[str, Any]:
    """Pin the raw engine result before any adapter metadata, exactly once."""
    if not isinstance(result.get("graph"), dict):
        return bounded_response(result)
    stored_result = {**result, "replayed": False}
    return bounded_response({
        **result,
        "full_result_sha256": canonical_sha256(stored_result),
        "full_result_representation": (
            "gzip bundle.members[0].content (JSON string).result with replayed=false"
            if result.get("recovery_artifact", {}).get("format") == "gzip-terminal-bundle-v1"
            else "journal.result with replayed=false"
        ),
        "full_result_bytes": len(_encoded(stored_result)),
        "recovery_artifact_root": str(state),
    })


def _read_regular(path: Path, maximum: int) -> bytes:
    _plain_file(path)
    with open(path, "rb", opener=lambda p, flags: os.open(p, flags | os.O_NOFOLLOW | os.O_NONBLOCK)) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise GraphEntryError("GRAPH_RECOVERY_FILE_IDENTITY_INVALID")
        if before.st_size > maximum:
            raise GraphEntryError("GRAPH_RECOVERY_READ_LIMIT")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if (len(data) > maximum or before.st_size != len(data)
                or (before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_ino, after.st_size, after.st_mtime_ns)):
            raise GraphEntryError("GRAPH_RECOVERY_READ_CHANGED")
        return data


def _effect_observations(request: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    """Observe only admitted exact targets; observations do not authorize replay."""
    targets = request["jspace"].get("declared_targets", [])
    if not isinstance(targets, list) or len(targets) > 128:
        raise GraphEntryError("GRAPH_RECOVERY_TARGET_BUDGET")
    result = []
    remaining = 16 * 1024 * 1024
    scopes = request["jspace"].get("read_scopes", [])
    for relative in targets:
        row = {"path": relative}
        try:
            _scope_rule(root, relative)
            if relative.endswith("/**") or not any(
                relative == s or (s.endswith("/**") and
                                 (relative == s[:-3] or relative.startswith(s[:-3] + "/")))
                for s in scopes
            ):
                raise GraphEntryError("GRAPH_RECOVERY_TARGET_NOT_EXACT_READ_SCOPE")
            target = root / relative
            if target.resolve() != target:
                raise GraphEntryError("GRAPH_RECOVERY_TARGET_SYMLINK")
            data = _read_regular(target, min(8 * 1024 * 1024, remaining))
            remaining -= len(data)
            row.update(state="observed", bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        except FileNotFoundError:
            row["state"] = "absent"
        except (OSError, GraphEntryError) as exc:
            row.update(state="unobserved", error=str(exc))
        result.append(row)
    return result


def _record_interruption(
    request: dict[str, Any], settings: GraphSettings, state: Path,
    outcome: dict[str, Any], supervisor_pid: int,
) -> dict[str, Any]:
    """Converge a dead engine's running journal, never a command's unknown outcome."""
    scope = outcome.get("process_scope", {})
    if (scope.get("supervisor_pid") != supervisor_pid or scope.get("engine_reaped") is not True
            or scope.get("no_live_descendants") is not True or scope.get("cleanup_error") is not None):
        return {"status": "preserved", "error": "GRAPH_RECOVERY_PROCESS_SCOPE_UNPROVEN"}
    key = canonical_sha256([request["task_id"], request["call_id"]])
    path = state / f"{key}.json"
    staging = state / f"{key}.reconciliation.tmp"
    created = False
    try:
        with ExitStack() as stack:
            # Same lock order and directory locks as the existing Rust engine.
            for directory in (settings.dcf_root.resolve(), state):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if path.with_suffix(".tmp").exists():
                raise GraphEntryError("GRAPH_RECOVERY_JOURNAL_STAGING_PRESERVED")
            raw = _read_regular(path, MAX_ENGINE_OUTPUT)
            journal = json.loads(raw, object_pairs_hook=_object)
            if (not isinstance(journal, dict) or journal.get("task_id") != request["task_id"]
                    or journal.get("call_id") != request["call_id"]
                    or journal.get("request_digest") != canonical_sha256(request)):
                raise GraphEntryError("GRAPH_RECOVERY_JOURNAL_IDENTITY_MISMATCH")
            if journal.get("status") not in {"running", "awaiting_process_closeout"}:
                return {"status": "preserved", "journal_status": journal.get("status"),
                        "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
            observations = _effect_observations(request, settings.dcf_root.resolve())
            journal.update(status="interrupted", effect_state="unproven", retry_safe=False)
            journal["interruption"] = {
                "prior_journal_sha256": hashlib.sha256(raw).hexdigest(),
                "process_scope": scope, "engine_exit_code": outcome.get("exit_code"),
                "failure": outcome.get("failure") or "GRAPH_ENGINE_NO_RESULT",
                "declared_target_observations": observations,
                "node_states_preserved": True,
                "effect_outcome_inferred_from_observations": False,
            }
            encoded = _encoded(journal)
            with staging.open("xb") as output:
                created = True
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            if _read_regular(path, MAX_ENGINE_OUTPUT) != raw:
                raise GraphEntryError("GRAPH_RECOVERY_JOURNAL_CHANGED")
            os.replace(staging, path)
            created = False
            os.fsync(fd)
            if _read_regular(path, MAX_ENGINE_OUTPUT) != encoded:
                raise GraphEntryError("GRAPH_RECOVERY_PUBLICATION_READBACK_FAILED")
            return {"status": "recorded", "path": str(path),
                    "sha256": hashlib.sha256(encoded).hexdigest(), "effect_state": "unproven",
                    "retry_safe": False, "declared_target_observations": observations}
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "preserved", "error": str(exc), "retry_safe": False}
    finally:
        if created:
            staging.unlink()


async def execute(
    payload: dict[str, Any], settings: GraphSettings,
    verify_freshness: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    if len(_encoded(payload)) > MAX_INPUT:
        raise GraphEntryError("GRAPH_ENTRY_INPUT_LIMIT")
    request, context = verify_context(payload, settings, verify_freshness)
    if sys.platform == "darwin" and os.environ.get("CODEX_SANDBOX") == "seatbelt":
        # Native marks its sandboxed commands; reinitializing Seatbelt there is
        # denied by macOS. Absence of this hint does not waive our own sandbox.
        raise GraphEntryError("GRAPH_HOST_SEATBELT_REENTRY_UNSUPPORTED")
    _plain_file(settings.engine)
    if _digest_file(settings.engine) != settings.engine_sha256:
        raise GraphEntryError("GRAPH_ENGINE_DIGEST_MISMATCH")
    state = _artifact_state(settings, request["task_id"])
    profile = sandbox_profile(request, settings, state)
    # Fingerprints are live, not a freshness label or an expiry approximation.
    verify_freshness(request["jspace"])
    supervision_timeout = request["timeout_ms"] / 1000 + 2
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-B",
        str(Path(__file__).with_name("graph_process.py").resolve()),
        "--engine", str(settings.engine), "--state-dir", str(state),
        "--parent-pid", str(os.getpid()), "--timeout", str(supervision_timeout),
        "--max-stdout", str(MAX_ENGINE_OUTPUT), "--max-stderr", str(MAX_RESPONSE),
        cwd=settings.dcf_root,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(state),
             "TMPDIR": str(state), "LANG": "en_US.UTF-8"},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    assert process.stdin is not None
    readers = [asyncio.create_task(_bounded_read(process.stdout, MAX_ENGINE_OUTPUT + MAX_RESPONSE)),
               asyncio.create_task(_bounded_read(process.stderr, MAX_RESPONSE))]
    gathered = asyncio.gather(*readers)
    try:
        process.stdin.write(_encoded(request))
        await process.stdin.drain()
        process.stdin.close()
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(gathered), supervision_timeout + 23,
        )
        await process.wait()
    except BaseException:
        await asyncio.shield(_stop(process))
        try:
            stopped_stdout, _ = await asyncio.wait_for(asyncio.shield(gathered), 5)
            stopped = json.loads(stopped_stdout, object_pairs_hook=_object)
            if isinstance(stopped, dict):
                _record_interruption(request, settings, state, stopped, process.pid)
        except (Exception, asyncio.CancelledError):
            pass  # Missing cleanup evidence leaves the original unknown journal intact.
        for reader in readers:
            reader.cancel()
        gathered.cancel()
        await asyncio.gather(gathered, *readers, return_exceptions=True)
        raise
    try:
        outcome = json.loads(stdout, object_pairs_hook=_object)
        if not isinstance(outcome, dict):
            raise ValueError("object required")
    except ValueError:
        # Losing the protocol does not prove that the engine never ran. Keep
        # exact observations, but do not expose untrusted child output or retry.
        return {
            **_failure(GraphEntryError("GRAPH_SUPERVISOR_RESULT_INVALID_EFFECT_UNSETTLED")),
            "task_id": request["task_id"], "call_id": request["call_id"],
            "request_digest": canonical_sha256(request),
            "supervisor_observation": {
                "pid": process.pid, "exit_code": process.returncode,
                "stdout_bytes": len(stdout), "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_bytes": len(stderr), "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            },
        }
    scope = outcome.get("process_scope", {})
    result = outcome.get("engine_result")
    cleanup_proven = (scope.get("supervisor_pid") == process.pid
                      and scope.get("engine_reaped") is True
                      and scope.get("no_live_descendants") is True
                      and scope.get("cleanup_error") is None)
    if process.returncode != 0 or not cleanup_proven or outcome.get("failure") or result is None:
        recovery = _record_interruption(request, settings, state, outcome, process.pid)
        return bounded_response({"status": "blocked", "error": outcome.get("failure") or (
                    "GRAPH_ENGINE_NO_RESULT" if cleanup_proven else "GRAPH_PROCESS_CLEANUP_UNPROVEN"),
                "task_id": request["task_id"], "call_id": request["call_id"],
                "request_digest": canonical_sha256(request), "process_scope": scope,
                "exit_code": outcome.get("exit_code"), "effect_state": "unproven",
                "stderr_sha256": outcome.get("stderr_sha256", hashlib.sha256(stderr).hexdigest()),
                "retry_safe": False, "interruption_reconciliation": recovery})
    if not isinstance(result, dict):
        raise GraphEntryError("GRAPH_ENGINE_RESULT_INVALID_EFFECT_UNSETTLED")
    if result.get("status") == "completed" and (
        outcome.get("exit_code") != 0 or result.get("task_id") != request["task_id"]
        or result.get("call_id") != request["call_id"]
        or result.get("request_digest") != canonical_sha256(request)
        or result.get("process_closeout_required") is True
    ):
        raise GraphEntryError("GRAPH_ENGINE_RESULT_IDENTITY_INVALID_EFFECT_UNSETTLED")
    result = compact_result(result, state)
    result["process_scope"] = scope
    result["context_semantic_sha256"] = context["semantic_sha256"]
    result["integration_profile"] = "optional-genuine-command-graph"
    result["execution_boundary"] = "macos-seatbelt-plus-jspace-not-native-per-node-approval"
    result["caller_task_binding"] = "launcher"
    result["workspace_write_capability_exposed"] = bool(request["jspace"]["write_scopes"])
    return bounded_response(result)


def _failure(error: Exception) -> dict[str, Any]:
    code = getattr(error, "code", None) or str(error) or type(error).__name__
    return bounded_response({"status": "blocked", "error": code, "retry_safe": False,
                             "effect_state": "unproven", "fallback_performed": False})


def _native_caller(settings: GraphSettings) -> str:
    """Bind shell entry modes to the host, not a model-supplied CLI override."""
    return _native_task(settings.caller_task_id)


def _native_task(expected: str | None) -> str:
    caller = os.environ.get("CODEX_THREAD_ID")
    if not caller or not _IDENTITY.fullmatch(caller):
        raise GraphEntryError("GRAPH_NATIVE_CALLER_ENV_REQUIRED")
    if expected != caller:
        raise GraphEntryError("GRAPH_NATIVE_CALLER_ENV_MISMATCH")
    return caller


def _auto_route(request: dict[str, Any], settings: GraphSettings) -> tuple[str, str]:
    """Select within a verified graph candidate; this never grants Native effects."""
    call_id = request.get("call_id")
    if (request.get("schema_version") != "codex_tura_graph_v1"
            or not isinstance(call_id, str) or not _IDENTITY.fullmatch(call_id)):
        raise GraphEntryError("GRAPH_AUTO_REQUEST_IDENTITY_INVALID")
    graph = request.get("graph")
    commands = graph.get("commands") if isinstance(graph, dict) else None
    if (not isinstance(commands, list) or not 1 <= len(commands) <= 20
            or any(not isinstance(node, dict) for node in commands)):
        raise GraphEntryError("GRAPH_AUTO_COMMANDS_INVALID")

    # Never send an existing graph attempt to an unjournaled Native path. Read
    # only exact call locators; do not create a second selection/recovery store.
    root = settings.artifact_root.resolve(strict=True)
    state = root / "tura-graph" / request["task_id"]
    for directory in (root / "tura-graph", state):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            break
        if not stat.S_ISDIR(info.st_mode):
            raise GraphEntryError("GRAPH_ARTIFACT_DIRECTORY_INVALID")
    else:
        key = canonical_sha256([request["task_id"], call_id])
        for suffix in (".json", ".tmp", ".json.gz", ".json.gz.tmp", ".reconciliation.tmp"):
            try:
                (state / (key + suffix)).lstat()
            except FileNotFoundError:
                continue
            return "graph", "existing_call_requires_graph_recovery"

    contract = request["jspace"]
    if request.get("preconditions", {}) != {} or contract.get("write_scopes") != []:
        return "graph", "preconditions_or_write_scope_require_graph"
    operations = contract.get("allowed_operations")
    if (not isinstance(operations, list) or not operations
            or any(op not in {"read", "command"} for op in operations)):
        return "graph", "non_read_operations_require_graph_admission"
    if set(graph) != {"commands"}:
        return "graph", "graph_controls_require_graph"
    templates = contract.get("command_templates")
    if not isinstance(templates, list) or not templates:
        return "graph", "command_policy_requires_graph_admission"
    read_argv = []
    for template in templates:
        if (not isinstance(template, dict) or template.get("effects") != ["read"]
                or not isinstance(template.get("argv"), list)
                or not template["argv"]
                or any(not isinstance(arg, str) for arg in template["argv"])):
            return "graph", "command_policy_requires_graph_admission"
        read_argv.append(template["argv"])
    ids: set[str] = set()
    steps: list[int] = []
    for node in commands:
        node_id = node.get("id")
        if (not isinstance(node_id, str) or not _IDENTITY.fullmatch(node_id)
                or node_id in ids):
            raise GraphEntryError("GRAPH_AUTO_NODE_IDENTITY_INVALID")
        ids.add(node_id)
        if (set(node) != {"id", "step", "command_type", "command_line"}
                or type(node["step"]) is not int or node["step"] < 1
                or node["command_type"] != "shell_command"):
            return "graph", "ordered_or_specialized_nodes_require_graph"
        steps.append(node["step"])
        line = node["command_line"]
        if not isinstance(line, str) or not line.strip():
            raise GraphEntryError("GRAPH_AUTO_COMMAND_LINE_INVALID")
        if "#@#" in line:
            return "graph", "output_bindings_require_graph"
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            raise GraphEntryError("GRAPH_AUTO_COMMAND_LINE_INVALID") from exc
        if argv not in read_argv:
            return "graph", "command_policy_requires_graph_admission"
    if any(step != 1 for step in steps):
        if steps != list(range(1, len(steps) + 1)):
            return "graph", "ordered_or_specialized_nodes_require_graph"
        return "native", "ordered_read_only_commands_prefer_native_batching"
    return "native", "independent_reads_prefer_native_batching"


async def auto_execute(
    payload: dict[str, Any], settings: GraphSettings,
    verify_freshness: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Execute a selected graph, or return a non-executing Native recommendation.

    This is not UI interception, Native tool dispatch, or a cross-route lease.
    The host must retain task ownership and authorize any later Native call.
    """
    _native_caller(settings)
    if len(_encoded(payload)) > MAX_INPUT:
        raise GraphEntryError("GRAPH_ENTRY_INPUT_LIMIT")
    request, context = verify_context(payload, settings, verify_freshness)
    route, reason = _auto_route(request, settings)
    selection = {"mode": "automatic", "selected_route": route, "reason": reason,
                 "authorization_granted": False, "fallback_performed": False}
    if route == "graph":
        # Keep the exact request/call identity and all execution-time checks.
        result = await execute(payload, settings, verify_freshness)
        return bounded_response({**result, "route_selection": selection})
    return {"status": "native_required", "task_id": request["task_id"],
            "call_id": request["call_id"], "request_digest": canonical_sha256(request),
            "context_semantic_sha256": context["semantic_sha256"],
            "route_selection": selection, "execution_started": False,
            "requires_native_tool_call": True, "retry_safe": False,
            "prior_effect_state": "not_inferred", "call_identity_reserved": False}


async def invoke_mcp(payload: dict[str, Any], settings: GraphSettings) -> dict[str, Any]:
    """Explicit MCP compatibility route; Native shells may use execute directly.

    This is not global MCP registration or an independent model/task loop.
    Never manufacture task identity from the request when the host omitted it.
    """
    caller = _native_caller(settings)
    if len(_encoded(payload)) > MAX_INPUT or set(payload) != {"request", "context"}:
        raise GraphEntryError("GRAPH_ENTRY_FIELDS_OR_BUDGET_INVALID")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "codex_collaboration_harness.graph_entry", "serve",
              "--engine", str(settings.engine), "--engine-sha256", settings.engine_sha256,
              "--dcf-root", str(settings.dcf_root), "--artifact-root", str(settings.artifact_root),
              "--caller-task-id", caller],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(settings.artifact_root),
             "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
             "PYTHONDONTWRITEBYTECODE": "1", "LANG": "en_US.UTF-8"},
    )
    async with asyncio.timeout(155):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                names = [tool.name for tool in (await client.list_tools()).tools]
                if names != ["nokiy_graph_execute"]:
                    raise GraphEntryError("GRAPH_MCP_INVENTORY_MISMATCH")
                answer = await client.call_tool("nokiy_graph_execute", payload)
                result = answer.structuredContent
                if answer.isError or not isinstance(result, dict):
                    raise GraphEntryError("GRAPH_MCP_RESULT_INVALID_EFFECT_UNSETTLED")
    result["host_invocation"] = "native-shell-task-bound-stdio-mcp"
    result["direct_native_mcp_registration"] = False
    result["mcp_transport_used"] = True
    return bounded_response(result)


def _native_mcp_settings(settings: GraphSettings, metadata: Any) -> GraphSettings:
    """Bind one request to host-supplied MCP metadata, never tool arguments.

    The local stdio host is the trust boundary, not a signature on metadata.
    No inherited environment or previous request supplies a missing identity.
    """
    if settings.caller_task_id is not None:
        raise GraphEntryError("GRAPH_NATIVE_MCP_STATIC_CALLER_FORBIDDEN")
    if hasattr(metadata, "model_dump"):
        metadata = metadata.model_dump()
    caller = metadata.get("threadId") if isinstance(metadata, dict) else None
    if not isinstance(caller, str) or not _IDENTITY.fullmatch(caller):
        raise GraphEntryError("GRAPH_NATIVE_MCP_TASK_METADATA_REQUIRED")
    return replace(settings, caller_task_id=caller)


def prepare_graph(
    specification: dict[str, Any], settings: GraphSettings, boundary: DcfBoundary,
) -> dict[str, Any]:
    """Compile with the existing DCF owner; create no execution or lifecycle state."""
    required = {"call_id", "surface_id", "mission", "action", "graph", "timeout_ms"}
    if (not isinstance(specification, dict) or len(_encoded(specification)) > MAX_INPUT
            or not required <= specification.keys() <= required | {"preconditions"}):
        raise GraphEntryError("GRAPH_PREPARE_SPECIFICATION_INVALID")
    call_id, surface = specification["call_id"], specification["surface_id"]
    if any(not isinstance(value, str) or not _IDENTITY.fullmatch(value)
           for value in (call_id, surface, settings.caller_task_id)):
        raise GraphEntryError("GRAPH_PREPARE_IDENTITY_INVALID")
    mission = specification["mission"]
    mission_keys = {"mission_id", "mode", "current_predicate", "objective"}
    if (not isinstance(mission, dict) or set(mission) != mission_keys
            or any(not isinstance(value, str) or not value.strip() for value in mission.values())):
        raise GraphEntryError("GRAPH_PREPARE_MISSION_INVALID")
    action = specification["action"]
    if not isinstance(action, dict) or "mission" in action:
        raise GraphEntryError("GRAPH_PREPARE_ACTION_INVALID")
    timeout = specification["timeout_ms"]
    if type(timeout) is not int or not 1 <= timeout <= 120_000:
        raise GraphEntryError("GRAPH_ENTRY_BUDGET_INVALID")
    bound_action = {**action, "mission": {**mission, "task_id": settings.caller_task_id}}
    contract, context = boundary.compile(surface, bound_action)
    request = {
        "schema_version": "codex_tura_graph_v1", "task_id": settings.caller_task_id,
        "call_id": call_id, "workspace": str(settings.dcf_root.resolve(strict=True)),
        "expires_at_unix": int(time.time()) + 180, "timeout_ms": timeout,
        "jspace": contract, "graph": specification["graph"],
    }
    if "preconditions" in specification:
        request["preconditions"] = specification["preconditions"]
    payload = {"request": request, "context": context}
    verify_context(payload, settings, boundary.verify)
    # Replay must use this unchanged envelope, not compile a new expiry/digest.
    return payload


def build_mcp_server(
    settings: GraphSettings, boundary: DcfBoundary, *, native_metadata: bool = False,
) -> Any:
    # Optional official SDK, not another hand-written protocol or network service.
    from mcp.server.fastmcp import Context, FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("nokiy-graph")

    async def nokiy_graph_execute(
        request: dict[str, Any], context: dict[str, Any], ctx: Any,
    ) -> dict[str, Any]:
        """Execute a predecided bounded graph in the caller's existing authorized scope.

        Use native tools for exploration or a next step requiring reasoning.
        Supply a verified DCF task_context_capsule_v1 and jspace_contract_v2.
        Internal nodes do NOT inherit individual Native Codex tool approvals.
        Never retry an uncertain result or create a fresh call_id to evade it.
        """
        try:
            bound = (_native_mcp_settings(settings, ctx.request_context.meta)
                     if native_metadata else settings)
            result = await execute({"request": request, "context": context}, bound, boundary.verify)
            if native_metadata:
                result["caller_task_binding"] = "mcp-request-metadata-threadId"
                result["host_invocation"] = "host-managed-task-bound-stdio-mcp"
                result["mcp_transport_used"] = True
                result["mcp_server_pid"] = os.getpid()
            return bounded_response(result)
        except Exception as exc:
            return _failure(exc)

    # Resolve this lazy optional SDK type before registration, so Context is
    # injected by the server and never exposed as a model-controlled argument.
    nokiy_graph_execute.__annotations__["ctx"] = Context
    server.add_tool(nokiy_graph_execute, annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False,
    ))
    if native_metadata:
        async def nokiy_graph_prepare(specification: dict[str, Any], ctx: Any) -> dict[str, Any]:
            """Compile a bounded graph using existing DCF and this host's task identity.

            Specification contains call_id, surface_id, mission (mission_id,
            mode, current_predicate, objective; no task_id), action (the existing
            DCF action fields), graph and timeout_ms; optional preconditions.
            This reads context only, grants no authority and executes no nodes.
            Pass the returned request/context unchanged to nokiy_graph_execute.
            Reuse that exact envelope for cached replay; do not prepare it again.
            """
            try:
                bound = _native_mcp_settings(settings, ctx.request_context.meta)
                return prepare_graph(specification, bound, boundary)
            except Exception as exc:
                return _failure(exc)

        nokiy_graph_prepare.__annotations__["ctx"] = Context
        server.add_tool(nokiy_graph_prepare, annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False,
        ))
    return server


def serve(settings: GraphSettings, boundary: DcfBoundary, *, native_metadata: bool = False) -> None:
    server = build_mcp_server(settings, boundary, native_metadata=native_metadata)
    server.run(transport="stdio")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "auto", "execute", "serve", "serve-native", "invoke",
                                        "plan-retirement", "read-result"))
    parser.add_argument("--call-id")
    parser.add_argument("--request-digest")
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--engine-sha256")
    parser.add_argument("--dcf-root", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--config", type=Path, default=(
        Path.home() / ".config/codex-collaboration-harness/graph.json"
    ))
    parser.add_argument("--caller-task-id")
    args = parser.parse_args(argv)
    try:
        read_only = args.mode in ("read-result", "plan-retirement")
        caller = (None if args.mode == "serve-native" else
                  args.caller_task_id if args.caller_task_id is not None else os.environ.get("CODEX_THREAD_ID"))
        if read_only:
            _native_task(caller)
        settings_fields = ("engine", "engine_sha256", "dcf_root", "artifact_root")
        required_fields = ("artifact_root",) if read_only else settings_fields
        if any(getattr(args, key) is None for key in required_fields):
            _plain_file(args.config)
            config = decode(args.config.read_bytes())
            if not set(required_fields) <= config.keys() <= set(settings_fields) or any(
                not isinstance(config[key], str) or not config[key].strip()
                for key in required_fields
            ):
                raise GraphEntryError("GRAPH_INSTALL_SETTINGS_INVALID")
            for key in required_fields:
                if getattr(args, key) is None:
                    setattr(args, key, config[key] if key == "engine_sha256" else Path(config[key]))
        if args.mode == "serve-native" and args.caller_task_id is not None:
            raise GraphEntryError("GRAPH_NATIVE_MCP_STATIC_CALLER_FORBIDDEN")
        if args.mode == "read-result":
            from .graph_retention import read_completed_call

            result = read_completed_call(args.artifact_root, caller,
                                         args.call_id, args.request_digest)
            print(_encoded(result).decode())
            return 0 if result.get("status") == "retrieved" else 2
        if args.mode == "plan-retirement":
            from .graph_retention import prepare_task_retirement

            candidate = prepare_task_retirement(args.artifact_root, caller)
            print(json.dumps({**candidate.report, "host_invocation": "native-shell-task-bound-retirement-plan",
                              "execution_started": False, "mcp_transport_used": False}, ensure_ascii=False))
            return 0  # A read-only plan, never task completion or cleanup admission.
        settings = GraphSettings(args.engine, args.engine_sha256, args.dcf_root,
                                 args.artifact_root, caller)
        if args.mode in ("serve", "serve-native"):
            serve(settings, DcfBoundary(settings.dcf_root), native_metadata=args.mode == "serve-native")
            return 0
        _native_caller(settings)
        payload = decode(sys.stdin.buffer.read(MAX_INPUT + 1))
        if args.mode == "prepare":
            prepared = prepare_graph(payload, settings, DcfBoundary(settings.dcf_root))
            # Emit the unchanged execution envelope, not a completion receipt.
            print(_encoded(prepared).decode())
            return 0
        if args.mode == "invoke":
            result = asyncio.run(invoke_mcp(payload, settings))
        else:
            executor = auto_execute if args.mode == "auto" else execute
            result = asyncio.run(executor(payload, settings, DcfBoundary(settings.dcf_root).verify))
            result["host_invocation"] = ("native-shell-task-bound-auto-selection"
                                         if args.mode == "auto"
                                         else "native-shell-task-bound-direct-graph")
            result["direct_native_mcp_registration"] = False
            result["mcp_transport_used"] = False
        result = bounded_response(result)
        print(_encoded(result).decode())
        if result.get("status") == "native_required":
            return 3  # Recommendation only; no execution success or automatic fallback.
        return 0 if result.get("status") == "completed" else 2
    except (Exception, KeyboardInterrupt) as exc:
        print(_encoded(_failure(exc)).decode())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
