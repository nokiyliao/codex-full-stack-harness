# SPDX-License-Identifier: MIT
"""Bounded, fail-closed coordination of independent, already prepared Direct requests.

The plan is an externally SHA-bound input, not an execution lease or a receipt.
Only the individual create-only run directories and terminal receipts are authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import embedded_nokiy as caller
from .result_inspection import INSPECTION_SUMMARY_FILE, project_terminal, retain_inspection_summary

SCHEMA = "nokiy_direct_batch_plan_v1"
RESULT_SCHEMA = "nokiy_direct_batch_result_v1"
CODE = "NOKIY_BATCH_INVALID"
MAX_PLAN_MEMBERS = 64
MAX_SCRATCH_ENTRIES = 4096


def _fail(detail: str) -> None:
    raise caller.EmbeddedNokiyError(CODE, detail)


def worker_capacity(config_path: Path | None = None) -> int:
    """Use the host's configured capacity without creating another policy store."""
    path = config_path or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    try:
        agents = tomllib.loads(path.read_text()).get("agents", {})
    except FileNotFoundError:
        agents = {}
    except (OSError, ValueError) as error:
        _fail(f"worker capacity configuration unreadable: {error}")
    if not isinstance(agents, dict):
        _fail("worker capacity agents configuration must be a table")
    if agents.get("enabled") is False:
        return 1
    limit = agents.get("max_concurrent_threads_per_session", agents.get("max_threads", 5))
    if type(limit) is not int or not 1 <= limit <= MAX_PLAN_MEMBERS:
        _fail("worker capacity must be an integer from 1 through 64")
    return limit


@dataclass(frozen=True)
class Member:
    path: Path
    file_sha256: str
    request_id: str
    artifact_root: Path


def _json_snapshot(path: Path, *, limit: int) -> tuple[Any, bytes]:
    """Decode only the bounded bytes opened from one plain, regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                _fail("JSON snapshot is not a regular file")
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            _fail("JSON snapshot exceeds byte limit")
        return json.loads(raw, object_pairs_hook=caller._unique_object,
                          parse_constant=caller._invalid_constant), raw
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        _fail(f"invalid JSON snapshot: {error}")


def load_prepared_request(path: Path) -> tuple[caller.EmbeddedNokiyRequest, bytes]:
    document, raw = _json_snapshot(path, limit=caller.MAX_REQUEST_BYTES)
    if not isinstance(document, dict):
        _fail("prepared request must be an object")
    # Reject untyped selection companions, not the supported prepared marker.
    preparation = document.get("preparation")
    if (any("model_selection" in key and key != "model_selection" for key in document)
            or isinstance(preparation, dict)
            and any("model_selection" in key for key in preparation)):
        _fail("unbound model selection preparation")
    from .model_topology import decode_prepared_snapshot
    request, _ = decode_prepared_snapshot(path, document, raw)
    return request, raw


def _load_plan(path: Path, sha256: str) -> tuple[tuple[Member, ...], int]:
    caller._require_sha256("plan_sha256", sha256)
    path = caller._plain_path(path, name="plan", directory=False)
    plan, raw = _json_snapshot(path, limit=8192)
    if hashlib.sha256(raw).hexdigest() != sha256:
        _fail("plan bytes differ from the approved digest")
    if (not isinstance(plan, dict)
            or not {"schema_version", "members"} <= set(plan)
            or set(plan) - {"schema_version", "members", "max_parallel_workers"}
            or plan["schema_version"] != SCHEMA):
        _fail("unsupported plan schema")
    rows = plan["members"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PLAN_MEMBERS:
        _fail("batch must contain 1 through 64 bounded members")
    parallel = plan.get("max_parallel_workers", len(rows))
    if type(parallel) is not int or not 1 <= parallel <= len(rows):
        _fail("max_parallel_workers must be an integer from 1 through member count")
    members = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "request_path", "request_file_sha256", "request_id", "artifact_root"
        }:
            _fail("member keys differ")
        # Read-batch does not require the original request to remain available.
        raw_path = row["request_path"]
        if (not isinstance(raw_path, str) or not Path(raw_path).is_absolute()
                or ".." in Path(raw_path).parts):
            _fail("request_path must be absolute and traversal-free")
        request_id = row["request_id"]
        if not isinstance(request_id, str) or not caller.REQUEST_ID_PATTERN.fullmatch(request_id):
            _fail("invalid original request_id")
        members.append(Member(Path(raw_path), caller._require_sha256(
            "request_file_sha256", row["request_file_sha256"]), request_id,
            caller._plain_path(row["artifact_root"], name="artifact_root", directory=True)))
    if len({m.request_id for m in members}) != len(members):
        _fail("duplicate request IDs")
    if len({m.path for m in members}) != len(members):
        _fail("duplicate request paths")
    for index, left in enumerate(members):
        for right in members[index + 1:]:
            if _overlap({left.artifact_root}, {right.artifact_root}):
                _fail("overlapping artifact roots")
    return tuple(members), parallel


def load_plan(path: Path, sha256: str) -> tuple[Member, ...]:
    return _load_plan(path, sha256)[0]


def _scope_path(workspace: Path, name: Any) -> Path:
    if (not isinstance(name, str) or not name or name.startswith("/")
            or "\\" in name or any(part in {"", ".", ".."} for part in name.split("/"))
            or any(char in name for char in "*?[]{}")):
        _fail("non-exact workspace scope")
    path = workspace / name
    if any(part.is_symlink() for part in (path, *path.parents) if part != workspace and part.is_relative_to(workspace)):
        _fail("symlink in workspace scope")
    if path.is_dir():
        _fail("directory scope is not an exact file")
    return path


def _scratch_paths(root: Path) -> set[Path]:
    """Inspect allocated scratch effects without expanding source authority."""
    paths = {root}
    pending = [root]
    directories = set()
    try:
        while pending:
            directory = pending.pop()
            info = directory.lstat()
            identity = (info.st_dev, info.st_ino)
            if not stat.S_ISDIR(info.st_mode) or identity in directories:
                _fail("aliased or non-directory verifier scratch scope")
            directories.add(identity)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        _fail("verifier scratch contains symlink, hardlink or special file")
                    paths.add(path)
                    if len(paths) > MAX_SCRATCH_ENTRIES:
                        _fail("verifier scratch exceeds bounded entry limit")
    except OSError:
        _fail("verifier scratch identity unavailable")
    return paths


def _scopes(request: caller.EmbeddedNokiyRequest) -> tuple[set[Path], set[Path]]:
    # Reject v1, directory/glob grants, arbitrary commands and unrecognized
    # expansion. Only existing typed verifier grants add scratch effects.
    jspace = caller._bound_json(request.jspace_contract, code=CODE)
    if jspace.get("schema_version") != "jspace_contract_v2":
        _fail("only exact v2 J-Space is batchable")
    supported = {"schema_version", "repo_root", "dcf_generation", "provenance",
                 "matched_surface_ids", "subject_surface_matches", "focused_verifiers", "declared_targets",
                 "read_scopes", "write_scopes", "allowed_operations", "denied_operations",
                 "command_templates", "command_effect_policy", "expansion", "source_read",
                 "authorization_semantic_sha256", "content_sha256", "read_commands",
                 "verifier_commands", "verifier_artifact_root"}
    if set(jspace) - supported:
        _fail("unsupported J-Space grant fields")
    if jspace.get("repo_root") != str(request.workspace):
        _fail("J-Space workspace identity differs")
    operations = jspace.get("allowed_operations")
    if (not isinstance(operations, list) or any(not isinstance(op, str) for op in operations)
            or not set(operations) <= {"read", "create", "modify", "command"}
            or ("command" in operations and jspace.get("source_read") is not True)
            or jspace.get("command_templates") != [] or "read_commands" in jspace
            or "read_search_scopes" in jspace or "side_effects" in jspace
            or jspace.get("command_effect_policy") != "trusted_argv_effects_v1"
            or jspace.get("expansion") != {"mode": "exact_target_only",
                  "error_code": "JSPACE_EXPANSION_REQUIRED", "mutation_on_expansion": False}):
        _fail("unsupported command, operation or side-effect shape")
    denied = jspace.get("denied_operations", [])
    if (not isinstance(denied, list) or any(not isinstance(op, str) for op in denied)
            or set(operations).intersection(denied)):
        _fail("allowed and denied operations disagree")
    reads, writes, targets = (jspace.get(key) for key in (
        "read_scopes", "write_scopes", "declared_targets"))
    if any(not isinstance(value, list) for value in (reads, writes, targets)):
        _fail("missing exact scopes")
    read_paths = {_scope_path(request.workspace, name) for name in reads}
    write_paths = {_scope_path(request.workspace, name) for name in writes}
    target_paths = {_scope_path(request.workspace, name) for name in targets}
    # Compiler-provided subject evidence is bound to declared content, not a grant.
    if "subject_surface_matches" in jspace:
        matches = jspace["subject_surface_matches"]
        if (not isinstance(matches, dict) or set(matches) != set(targets)
                or any(not isinstance(rows, list) or any(
                    not isinstance(row, dict) or set(row) != {"surface_id", "owner_lane"}
                    or not all(isinstance(row[key], str) for key in ("surface_id", "owner_lane"))
                    for row in rows) for rows in matches.values())):
            _fail("invalid subject surface matches")
    if not write_paths <= target_paths:
        _fail("write scope outside declared targets")
    if (write_paths and not {"create", "modify"}.intersection(operations)
            or read_paths and "read" not in operations):
        _fail("scopes and operations disagree")
    if "verifier_commands" in jspace or "verifier_artifact_root" in jspace:
        if ("verifier_commands" not in jspace or "verifier_artifact_root" not in jspace
                or jspace["verifier_artifact_root"] != str(request.artifact_root)
                or not {"read", "command"}.issubset(operations) or "write" in denied):
            _fail("verifier requires read/command and its bound artifact root")
        from .local_context import _verifier_commands
        verifiers = _verifier_commands(jspace["verifier_commands"], request.workspace,
                                       request.artifact_root, reads)
        verifier_reads = {Path(command["argv"][0]) for command in verifiers}
        verifier_reads.update(Path(entry["path"]) for command in verifiers
                              for entry in command["pinned_files"])
        scratch_paths = set()
        for command in verifiers:
            scratch = Path(command["scratch_root"])
            if _overlap({scratch}, scratch_paths):
                _fail("overlapping verifier scratch scopes")
            scratch_paths.update(_scratch_paths(scratch))
        if _overlap(scratch_paths, {request.workspace}):
            _fail("verifier scratch and workspace overlap")
        if _overlap(verifier_reads, write_paths | scratch_paths):
            _fail("immutable verifier dependencies overlap writable scopes")
        # These are conflict dependencies/effects, not additional source grants
        # or command templates. Source create/modify denials remain unchanged.
        read_paths.update(verifier_reads)
        write_paths.update(scratch_paths)
    # Declared targets may be included in freshness snapshots even when they
    # are not explicit read grants. Treat them as dependencies, not new grants.
    return read_paths | target_paths, write_paths


def _overlap(left: set[Path], right: set[Path]) -> bool:
    def inode(path: Path) -> tuple[int, int] | None:
        try:
            info = path.stat()
        except FileNotFoundError:
            return None  # An uncreated output has no physical alias yet.
        except OSError:
            _fail("batch scope identity unavailable")
        return info.st_dev, info.st_ino

    identities = {path: inode(path) for path in left | right}
    left_inodes = {identities[path] for path in left if identities[path] is not None}
    right_inodes = {identities[path] for path in right if identities[path] is not None}
    # Scratch trees can contain many entries; preserve exact containment/inode
    # checks without a quadratic comparison of their descendants.
    return (bool(left & right or left_inodes & right_inodes)
            or any(parent in right for path in left for parent in path.parents)
            or any(parent in left for path in right for parent in path.parents))


def _project(member: Member, terminal: dict[str, Any]) -> dict[str, Any]:
    return project_terminal(terminal, member.artifact_root, member.request_id,
                            request_thread_id=terminal.get("native_thread_id"),
                            require_request_binding=True)


def _row(member: Member, *, terminal: dict[str, Any] | None = None,
         blocker: str | None = None) -> dict[str, Any]:
    inspection = terminal.get("result_inspection") if terminal is not None else None
    evidence_blocker = (inspection.get("first_blocker") if isinstance(inspection, dict)
                        and inspection.get("status") != "EVIDENCE_VERIFIED" else None)
    return {"request_path": str(member.path), "request_file_sha256": member.file_sha256,
            "request_id": member.request_id, "artifact_root": str(member.artifact_root),
            "status": terminal.get("status", "BLOCKED") if terminal is not None else "BLOCKED",
            "first_typed_blocker": (blocker if terminal is None else
                                    evidence_blocker or terminal.get("first_typed_blocker")),
            "cleanup_pass": terminal.get("cleanup_pass") if terminal is not None else None,
            "terminal": terminal}


def _aggregate(members: tuple[Member, ...], rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ordered = [rows[member.request_id] for member in members]
    def complete(row: dict[str, Any]) -> bool:
        terminal = row["terminal"]
        inspection = terminal.get("result_inspection") if isinstance(terminal, dict) else None
        return (row["status"] == "RESULT_AVAILABLE" and row["cleanup_pass"] is True
                and isinstance(inspection, dict) and inspection.get("status") == "EVIDENCE_VERIFIED")

    ok = all(complete(row) for row in ordered)
    return {"schema_version": RESULT_SCHEMA, "status": "RESULT_AVAILABLE" if ok else "BLOCKED",
            "first_typed_blocker": next((row["first_typed_blocker"] or "NOKIY_BATCH_MEMBER_INCOMPLETE"
                                         for row in ordered if not complete(row)), None),
            "members": ordered, "continuation_owner": "codex", "mission_acceptance": "parent_owned"}


def _read(member: Member) -> dict[str, Any]:
    run = member.artifact_root / member.request_id
    try:
        if run.is_symlink() or (run.exists() and not run.is_dir()):
            return _row(member, blocker="NOKIY_EMBEDDED_UNCERTAIN_PRIOR_ATTEMPT")
        if not run.exists():
            return _row(member, blocker="NOKIY_BATCH_NOT_STARTED")
        terminal = _project(member, caller.read_terminal(member.artifact_root, member.request_id))
        return _row(member, terminal={**terminal, "inspection_summary": None,
                                     "inspection_summary_omission": "READ_ONLY_RECOVERY"})
    except (caller.EmbeddedNokiyError, OSError, ValueError) as error:
        return _row(member, blocker=getattr(error, "code", "NOKIY_BATCH_READ_UNCERTAIN"))


def read_batch(path: Path, sha256: str) -> dict[str, Any]:
    members = load_plan(path, sha256)
    return _aggregate(members, {m.request_id: _read(m) for m in members})


_SHARED_SUMMARY_FIELDS = ("source_read_efficiency", "model_route", "reasoning_effort",
                          "requested_service_tier", "observed_service_tier")


def _compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Losslessly factor metadata; request identity and proof fields stay per member."""
    rows = [{**row} for row in summary["members"]]
    defaults = {}
    for key in _SHARED_SUMMARY_FIELDS:
        values = [json.dumps(row[key], sort_keys=True, ensure_ascii=True) for row in rows]
        if values and len(set(values)) == 1:
            defaults[key] = rows[0][key]
            for row in rows:
                row.pop(key)
    if rows and all(isinstance(row.get("usage"), dict) and "coverage" in row["usage"]
                    for row in rows):
        values = [json.dumps(row["usage"]["coverage"], sort_keys=True, ensure_ascii=True)
                  for row in rows]
        if len(set(values)) == 1:
            defaults["usage"] = {"coverage": rows[0]["usage"]["coverage"]}
            for row in rows:
                row["usage"] = {key: value for key, value in row["usage"].items()
                                if key != "coverage"}
    for row in rows:
        row.pop("terminal_path")
    references = [row.get("inspection_summary") for row in rows]
    factor_references = any(isinstance(ref, dict) for ref in references) and all(
        ref is None or isinstance(ref, dict) and ref.get("path") == str(
            Path(row["artifact_root"]) / row["request_id"] / INSPECTION_SUMMARY_FILE)
        for row, ref in zip(rows, references))
    if factor_references:
        for row, ref in zip(rows, references):
            if isinstance(ref, dict):
                row["inspection_summary"] = {key: value for key, value in ref.items() if key != "path"}
    return {**summary, "schema_version": "nokiy_direct_batch_summary_v2",
            "member_defaults": defaults, "members": rows,
            "terminal_path_rule": "artifact_root/request_id/terminal.json",
            **({"inspection_summary_path_rule": "artifact_root/request_id/inspection-summary.json"}
               if factor_references else {})}


def expand_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Restore v2 metadata for consumers; never inspect, execute or accept a result."""
    if summary.get("schema_version") == "nokiy_direct_batch_summary_v1":
        return summary
    if (summary.get("schema_version") != "nokiy_direct_batch_summary_v2"
            or summary.get("terminal_path_rule") != "artifact_root/request_id/terminal.json"
            or not isinstance(summary.get("member_defaults"), dict)
            or not isinstance(summary.get("members"), list)):
        _fail("invalid compact summary")
    summary_rule = summary.get("inspection_summary_path_rule")
    if summary_rule is not None and summary_rule != "artifact_root/request_id/inspection-summary.json":
        _fail("invalid inspection summary path rule")
    defaults = summary["member_defaults"]
    if (set(defaults) - set(_SHARED_SUMMARY_FIELDS) - {"usage"}
            or ("usage" in defaults and (not isinstance(defaults["usage"], dict)
                or set(defaults["usage"]) != {"coverage"}))):
        _fail("invalid shared summary metadata")
    rows = []
    for member in summary["members"]:
        if (not isinstance(member, dict) or "terminal_path" in member
                or not isinstance(member.get("artifact_root"), str)
                or not isinstance(member.get("request_id"), str)):
            _fail("invalid compact summary member")
        row = {**defaults, **member}
        if "usage" in defaults:
            if not isinstance(member.get("usage"), dict):
                _fail("invalid compact usage metadata")
            row["usage"] = {**defaults["usage"], **member["usage"]}
        row["terminal_path"] = str(Path(row["artifact_root"]) / row["request_id"] / "terminal.json")
        reference = member.get("inspection_summary")
        if summary_rule is not None and reference is not None:
            if not isinstance(reference, dict) or set(reference) != {"sha256", "bytes"}:
                _fail("invalid compact inspection summary reference")
            row["inspection_summary"] = {**reference, "path": str(
                Path(row["artifact_root"]) / row["request_id"] / INSPECTION_SUMMARY_FILE)}
        rows.append(row)
    return {**{key: value for key, value in summary.items()
               if key not in {"member_defaults", "terminal_path_rule", "inspection_summary_path_rule"}},
            "schema_version": "nokiy_direct_batch_summary_v1", "members": rows}


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """Opt-in CLI projection; raw terminals remain available via read-result."""
    def encoded(value: Any) -> bytes:
        # Match the actual CLI serialization, including its default spaces.
        return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("ascii")

    rows = []
    for row in result["members"]:
        terminal = row.get("terminal") or {}
        inspection = terminal.get("result_inspection") or {}
        text = terminal.get("result_text")
        rows.append({
            "request_id": row["request_id"], "artifact_root": row["artifact_root"],
            "status": row["status"], "first_typed_blocker": row["first_typed_blocker"],
            "cleanup_pass": row["cleanup_pass"],
            "model": terminal.get("model"),
            "model_route": terminal.get("model_route"),
            "reasoning_effort": terminal.get("reasoning_effort"),
            "requested_service_tier": terminal.get("requested_service_tier"),
            "observed_service_tier": terminal.get("observed_service_tier"),
            "usage": terminal.get("usage"),
            "result_text": text if isinstance(text, str) else None,
            "result_truncated": terminal.get("result_truncated", False),
            "evidence_status": inspection.get("status"),
            "evidence_counts": inspection.get("counts"),
            "evidence_first_blocker": inspection.get("first_blocker"),
            "source_read_efficiency": inspection.get("source_read_efficiency"),
            "terminal_path": str(Path(row["artifact_root"]) / row["request_id"] / "terminal.json"),
            "terminal_sha256": inspection.get("terminal_sha256"),
            **{key: terminal[key] for key in ("inspection_summary", "inspection_summary_diagnostic",
                                              "inspection_summary_omission") if key in terminal},
        })
    summary = {"schema_version": "nokiy_direct_batch_summary_v1",
               "status": result["status"],
               "first_typed_blocker": result["first_typed_blocker"],
               "members": rows, "continuation_owner": "codex",
               "mission_acceptance": "parent_owned"}
    # Trim only result text, one Unicode code point at a time. All identity and
    # evidence metadata survives unchanged, including the truncation marker.
    for row in rows:
        row["result_truncated"] = bool(row["result_truncated"])
    if len(encoded(summary)) > 8000:
        compact = _compact_summary(summary)
        if len(encoded(compact)) < len(encoded(summary)):
            summary = compact
            rows = summary["members"]
    while len(encoded(summary)) > 8000 and any(row["result_text"] for row in rows):
        largest = max(rows, key=lambda row: len(encoded(row["result_text"]))
                      if row["result_text"] else 0)
        text = largest["result_text"]
        largest["result_text"] = text[:max(0, len(text) // 2)]
        largest["result_truncated"] = True
    if len(encoded(summary)) > 8000:
        return {"schema_version": "nokiy_direct_batch_summary_v1",
                "status": "COMPACT_METADATA_TOO_LARGE",
                "first_typed_blocker": "NOKIY_BATCH_SUMMARY_TOO_LARGE",
                "recovery": "Use the printed original plan path and SHA with read-batch; use read-result for original terminals."}
    return summary


def run_batch(path: Path, sha256: str) -> dict[str, Any]:
    members, parallel = _load_plan(path, sha256)
    caller_thread = os.environ.get("CODEX_THREAD_ID")
    if not isinstance(caller_thread, str) or not caller.THREAD_ID_PATTERN.fullmatch(caller_thread):
        _fail("actual native thread ID unavailable")
    # Any existing run (including a successful terminal) makes this invocation
    # result-only. Never infer that a missing peer is safe to resend on reentry.
    if any((m.artifact_root / m.request_id).exists() or
           (m.artifact_root / m.request_id).is_symlink() for m in members):
        return _aggregate(members, {m.request_id: _read(m) for m in members})
    capacity = worker_capacity()
    if len(members) > capacity:
        _fail(f"batch has {len(members)} members but configured worker capacity is {capacity}")
    requests = []
    scopes = []
    workspace = None
    # Complete validation, including original preflight, before any model starts.
    for member in members:
        request, raw = load_prepared_request(member.path)
        if (hashlib.sha256(raw).hexdigest() != member.file_sha256
                or request.request_id != member.request_id
                or request.artifact_root != member.artifact_root
                or request.schema_version != caller.REQUEST_SCHEMA_VERSION
                or request.execution_profile != "direct"
                or request.native_thread_id != caller_thread
                or (workspace is not None and request.workspace != workspace)):
            _fail("prepared member identity, thread, profile or workspace differs")
        workspace = request.workspace
        if _overlap({member.artifact_root}, {workspace}):
            _fail("artifact root and workspace overlap")
        scopes.append(_scopes(request))
        ready = caller.preflight(request)
        if ready.get("status") != "READY":
            _fail("member preflight is not READY")
        requests.append(request)
    for index, (reads, writes) in enumerate(scopes):
        for other_index, (other_reads, other_writes) in enumerate(scopes[index + 1:], start=index + 1):
            if (_overlap(writes, {members[other_index].artifact_root})
                    or _overlap(other_writes, {members[index].artifact_root})):
                _fail("foreign member artifact ownership overlap")
            if (_overlap(writes, other_reads) or _overlap(writes, other_writes)
                    or _overlap(other_writes, reads)):
                _fail("cross-member read/write or write/write overlap")
    # Preflight is not a lease: a run appearing during validation is not retried.
    if any((m.artifact_root / m.request_id).exists() or
           (m.artifact_root / m.request_id).is_symlink() for m in members):
        return _aggregate(members, {m.request_id: _read(m) for m in members})

    if len(members) > worker_capacity():
        _fail("batch exceeds current configured worker capacity")

    def execute_one(member: Member, request: caller.EmbeddedNokiyRequest) -> dict[str, Any]:
        try:
            terminal = _project(member, caller.execute(request))
            terminal = retain_inspection_summary(terminal, member.artifact_root, member.request_id,
                                                 expected_thread_id=request.native_thread_id)
            return _row(member, terminal=terminal)
        except Exception as error:
            # A crash after create-only claim without a terminal is uncertain.
            return _row(member, blocker=getattr(error, "code", "NOKIY_BATCH_EXECUTION_UNCERTAIN"))

    # Share cancellation with every worker in this invocation, not other runs.
    with caller._cancellation_signals():
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [pool.submit(copy_context().run, execute_one, member, request)
                       for member, request in zip(members, requests)]
            rows = {member.request_id: future.result() for member, future in zip(members, futures)}
    return _aggregate(members, rows)
