# SPDX-License-Identifier: MIT
"""Bounded, fail-closed coordination of independent, already prepared Direct requests.

The plan is an externally SHA-bound input, not an execution lease or a receipt.
Only the individual create-only run directories and terminal receipts are authority.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import embedded_nokiy as caller
from . import postimage_context
from .graph_process import native_batch_scope
from .result_inspection import (
    INSPECTION_SUMMARY_FILE, MAX_PROJECTED_BYTES, project_terminal, retain_inspection_summary,
)

SCHEMA = "nokiy_direct_batch_plan_v1"
RESULT_SCHEMA = "nokiy_direct_batch_result_v1"
CODE = "NOKIY_BATCH_INVALID"
MAX_PLAN_MEMBERS = 64
MAX_WORKER_CAPACITY = 20
MAX_PLAN_BYTES = 32768
MAX_SCRATCH_ENTRIES = 4096
# Member change delivery: the parent reviews each worker's exact diff instead of
# rereading every edited file. Context only; never a grant, receipt or acceptance.
CHANGES_SCHEMA = "nokiy_member_changes_v1"
MAX_PREIMAGE_BYTES = 8 * 1024 * 1024
# Codex shows a tool output up to min(requested budget, the alias catalog truncation_policy).
# Nokiy aliases admit 64000 tokens (Codex estimates 4 bytes per token of this ASCII JSON), so
# a 128 KiB summary arrives whole; beyond the window the model sees a head/tail cut, which
# corrupts a diff and forces a reread.
MAX_BATCH_SUMMARY_BYTES = 128 * 1024
MAX_INLINE_DIFF_BYTES = 96 * 1024


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
    limit = agents.get("max_concurrent_threads_per_session",
                       agents.get("max_threads", MAX_WORKER_CAPACITY))
    if type(limit) is not int or not 1 <= limit <= MAX_WORKER_CAPACITY:
        _fail("worker capacity must be an integer from 1 through 20")
    return limit


@dataclass(frozen=True)
class Member:
    path: Path
    file_sha256: str
    request_id: str
    artifact_root: Path
    artifact_root_blocker: str | None = None


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


def _load_plan(path: Path, sha256: str, *, recovery: bool = False,
               post_batch_verifiers: list | None = None) -> tuple[tuple[Member, ...], int]:
    caller._require_sha256("plan_sha256", sha256)
    path = caller._plain_path(path, name="plan", directory=False)
    plan, raw = _json_snapshot(path, limit=MAX_PLAN_BYTES)
    if hashlib.sha256(raw).hexdigest() != sha256:
        _fail("plan bytes differ from the approved digest")
    if (not isinstance(plan, dict)
            or not {"schema_version", "members"} <= set(plan)
            or set(plan) - {"schema_version", "members", "max_parallel_workers", "post_batch_verifier"}
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
        raw_root = row["artifact_root"]
        if (not isinstance(raw_root, str) or not raw_root or "\0" in raw_root
                or not Path(raw_root).expanduser().is_absolute()
                or ".." in Path(raw_root).parts):
            _fail("artifact_root must be absolute and traversal-free")
        root = Path(raw_root).expanduser()
        blocker = None
        try:
            root = caller._plain_path(root, name="artifact_root", directory=True)
        except (caller.EmbeddedNokiyError, OSError, ValueError, RuntimeError):
            if not recovery:
                raise
            blocker = "NOKIY_BATCH_ARTIFACT_ROOT_UNAVAILABLE"
        members.append(Member(Path(raw_path), caller._require_sha256(
            "request_file_sha256", row["request_file_sha256"]), request_id, root, blocker))
    if len({m.request_id for m in members}) != len(members):
        _fail("duplicate request IDs")
    if len({m.path for m in members}) != len(members):
        _fail("duplicate request paths")
    for index, left in enumerate(members):
        for right in members[index + 1:]:
            # Invalid roots are never opened during recovery; their lexical
            # overlap still invalidates the plan, while healthy roots retain
            # the physical alias check.
            lexical_overlap = (left.artifact_root == right.artifact_root
                               or left.artifact_root in right.artifact_root.parents
                               or right.artifact_root in left.artifact_root.parents)
            if (lexical_overlap or (left.artifact_root_blocker is None
                                    and right.artifact_root_blocker is None
                                    and _overlap({left.artifact_root}, {right.artifact_root}))):
                _fail("overlapping artifact roots")
    if "post_batch_verifier" in plan:
        from .post_batch_verifier import validate
        verifier = validate(plan["post_batch_verifier"], tuple(members), recovery=recovery)
        if post_batch_verifiers is not None:
            post_batch_verifiers.append(verifier)
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


def _discovery_scopes(workspace: Path, jspace: dict) -> tuple[dict[str, Path], set[Path]]:
    if "read_commands" not in jspace:
        return {}, set()
    from .local_context import _directories, _read_commands
    policy = jspace["read_commands"]
    roots = policy.get("roots") if isinstance(policy, dict) else None
    if (not isinstance(roots, list) or not roots
            or any(not isinstance(root, str) for root in roots)
            or len(set(roots)) != len(roots)):
        _fail("discovery requires distinct bound directory roots")
    _directories(workspace, roots)
    if policy != _read_commands(roots):
        _fail("discovery policy/executable identity changed")
    patterns = {root + "/**": workspace / root for root in roots}
    reads = jspace.get("read_scopes")
    if not isinstance(reads, list) or any(pattern not in reads for pattern in patterns):
        _fail("discovery roots require matching read scopes")
    return patterns, {Path(policy[name]["path"]) for name in ("rg", "cat")}


def _scopes(request: caller.EmbeddedNokiyRequest, *, post_batch_verifier: bool = False) -> tuple[set[Path], set[Path]]:
    # Discovery roots are read dependencies, never expanded write grants.
    # Existing typed verifier grants alone add scratch effects.
    jspace = caller._bound_json(request.jspace_contract, code=CODE)
    if jspace.get("schema_version") != "jspace_contract_v2":
        _fail("only exact v2 J-Space is batchable")
    if "inspect_shell" in jspace:
        return _inspect_scopes(request, jspace)
    supported = {"schema_version", "repo_root", "dcf_generation", "provenance",
                 "matched_surface_ids", "subject_surface_matches", "focused_verifiers", "declared_targets",
                 "read_scopes", "write_scopes", "allowed_operations", "denied_operations",
                 "command_templates", "command_effect_policy", "expansion", "source_read",
                 "authorization_semantic_sha256", "content_sha256", "read_commands",
                 "verifier_commands", "verifier_artifact_root", "parent_shell"}
    if set(jspace) - supported:
        _fail("unsupported J-Space grant fields")
    if jspace.get("repo_root") != str(request.workspace):
        _fail("J-Space workspace identity differs")
    discovery, tools = _discovery_scopes(request.workspace, jspace)
    operations = jspace.get("allowed_operations")
    if (not isinstance(operations, list) or any(not isinstance(op, str) for op in operations)
            or not set(operations) <= {"read", "create", "modify", "command"}
            or ("command" in operations and jspace.get("source_read") is not True and not discovery
                and "parent_shell" not in jspace)
            or (discovery and not {"read", "command"}.issubset(operations))
            or jspace.get("command_templates") != []
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
    if any(not isinstance(name, str) for name in reads):
        _fail("read scopes must be strings")
    read_paths = {discovery[name] if name in discovery else _scope_path(request.workspace, name)
                  for name in reads} | tools
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
        from .local_context import _post_batch_artifact_root, _verifier_commands
        verifier_root = (_post_batch_artifact_root(request.workspace, request.artifact_root,
            jspace.get("verifier_commands"), operations, writes, targets)
            if post_batch_verifier else request.artifact_root)
        if ("verifier_commands" not in jspace or "verifier_artifact_root" not in jspace
                or jspace["verifier_artifact_root"] != str(verifier_root)
                or not {"read", "command"}.issubset(operations) or "write" in denied):
            _fail("verifier requires read/command and its bound artifact root")
        verifiers = _verifier_commands(jspace["verifier_commands"], request.workspace,
                                       verifier_root, reads, write_scopes=jspace.get("write_scopes") or [])
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


def _inspect_scopes(request: caller.EmbeddedNokiyRequest, jspace: dict) -> tuple[set[Path], set[Path]]:
    # The bound fence lets an inspect member read anything but write only its own artifact
    # root and the system temporary directories, where TMPDIR/TMPPREFIX point at its own
    # scratch. Members share the workspace, so it is the read dependency and the artifact
    # root the write effect: inspect members batch together, never beside a writer of that
    # workspace. caller.preflight still verifies the fence binding of every member.
    if (jspace.get("allowed_operations") != ["command", "read"]
            or jspace.get("repo_root") != str(request.workspace)
            or any(jspace.get(key) != [] for key in ("read_scopes", "write_scopes", "declared_targets",
                                                      "command_templates"))
            or {"read_commands", "verifier_commands", "verifier_artifact_root", "source_read"} & jspace.keys()):
        _fail("unsupported inspect shape")
    return {request.workspace}, {request.artifact_root}


def _check_discovery_aliases(reads: set[Path], writes: set[Path]) -> None:
    if not writes or not any(path.is_dir() for path in reads):
        return
    # A write alias can reach inside a discovery tree without lexical overlap.
    # Do not scan that tree merely to prove a multiply-linked write disjoint.
    for path in writes:
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            _fail("batch write identity unavailable")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            _fail("hardlinked write cannot be proven disjoint from discovery")


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


def _source_write_targets(request: caller.EmbeddedNokiyRequest) -> list[tuple[str, Path]]:
    """Exact source write grants only; verifier scratch and inspect members have none."""
    jspace = caller._bound_json(request.jspace_contract, code=CODE)
    names = jspace.get("write_scopes") if isinstance(jspace, dict) else None
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        return []
    return sorted((name, _scope_path(request.workspace, name)) for name in set(names))


def _read_plain(path: Path) -> bytes | None:
    """Bytes of a regular, non-symlink file; None when it does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("write target is not a regular file")
        return stream.read(MAX_PREIMAGE_BYTES + 1)


def _capture_preimages(request: caller.EmbeddedNokiyRequest) -> dict[str, bytes | None] | None:
    """Snapshot exact write targets immediately before a member starts; None if unavailable."""
    try:
        images, total = {}, 0
        for name, path in _source_write_targets(request):
            data = _read_plain(path)
            total += len(data or b"")
            if total > MAX_PREIMAGE_BYTES:
                return None
            images[name] = data
        return images
    except (caller.EmbeddedNokiyError, OSError, ValueError):
        return None


def _unified(name: str, before: bytes | None, after: bytes | None) -> tuple[str, int, int] | None:
    try:
        old = before.decode("utf-8") if before is not None else ""
        new = after.decode("utf-8") if after is not None else ""
    except UnicodeDecodeError:
        return None
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile="a/" + name if before is not None else "/dev/null",
                                 tofile="b/" + name if after is not None else "/dev/null", n=3)
    chunks, added, removed = [], 0, 0
    for line in lines:
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
        chunks.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(chunks), added, removed


def _changes_paths(member: Member) -> tuple[Path, Path]:
    stem = member.artifact_root / ("changes-" + member.request_id)
    return stem.with_suffix(".json"), stem.with_suffix(".diff")


def _create_only(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _record_changes(member: Member, request: caller.EmbeddedNokiyRequest,
                    preimages: dict[str, bytes | None] | None) -> dict[str, Any] | None:
    """Diff each exact write target against its pre-start snapshot, success or failure alike."""
    if preimages is None:
        return {"schema_version": CHANGES_SCHEMA, "status": "UNAVAILABLE", "reason": "preimage_capture_unavailable"}
    if not preimages:
        return None  # read-only member: no source write grant, nothing to deliver
    try:
        files, chunks = [], []
        for name, path in _source_write_targets(request):
            before, after = preimages.get(name), _read_plain(path)
            if before == after:
                continue
            row = {"path": name,
                   "change": "create" if before is None else "delete" if after is None else "modify",
                   "preimage_sha256": hashlib.sha256(before).hexdigest() if before is not None else None,
                   "postimage_sha256": hashlib.sha256(after).hexdigest() if after is not None else None}
            diff = _unified(name, before, after)
            if diff is None:
                row["binary"] = True
            else:
                row["added"], row["removed"] = diff[1], diff[2]
                chunks.append(diff[0])
            files.append(row)
        text = "".join(chunks).encode("utf-8")
        record = {"schema_version": CHANGES_SCHEMA, "status": "COMPLETE", "files": files,
                  "diff_sha256": hashlib.sha256(text).hexdigest(), "diff_bytes": len(text)}
        record_path, diff_path = _changes_paths(member)
        if text:
            _create_only(diff_path, text)
            record["diff_path"] = str(diff_path)
        _create_only(record_path, (json.dumps(record, sort_keys=True) + "\n").encode())
        return record
    except (caller.EmbeddedNokiyError, OSError, ValueError) as error:
        return {"schema_version": CHANGES_SCHEMA, "status": "UNAVAILABLE",
                "reason": type(error).__name__}


def _load_changes(member: Member) -> dict[str, Any] | None:
    """Recovery reads the published record only; it never recomputes a diff."""
    record_path, _ = _changes_paths(member)
    try:
        record, _ = _json_snapshot(record_path, limit=1024 * 1024)
    except caller.EmbeddedNokiyError:
        return None
    return record if isinstance(record, dict) and record.get("schema_version") == CHANGES_SCHEMA else None


def _diff_text(changes: dict[str, Any]) -> str | None:
    path = changes.get("diff_path")
    if not isinstance(path, str):
        return None
    try:
        data = _read_plain(Path(path))
    except OSError:
        return None
    if data is None or hashlib.sha256(data).hexdigest() != changes.get("diff_sha256"):
        return None
    return data.decode("utf-8")


_HUNK_HEADER = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_TEST_DIRECTORIES = frozenset({"test", "tests", "testing", "__tests__", "spec", "specs"})
_TEST_FILE = re.compile(r"^tests?_|^conftest\.py$|(?:_tests?|_spec|\.test|\.spec)\.[^.]+$")


def _json_cost(text: str) -> int:
    """Bytes a string adds inside the ASCII JSON summary, excluding its quotes (additive)."""
    return len(json.dumps(text, ensure_ascii=True)) - 2


def _is_test_path(path: str | None) -> bool:
    if path is None:
        return False
    parts = path.split("/")
    return bool(_TEST_DIRECTORIES.intersection(parts[:-1]) or _TEST_FILE.search(parts[-1]))


def _diff_lines(text: str) -> list[str]:
    return [line + "\n" for line in text[:-1].split("\n")]


def _diff_sections(text: str) -> list[tuple[str, str]] | None:
    """Split a member diff into (path, text) file sections by counting hunk lines.

    Counting lines, rather than matching '--- ' anywhere, keeps a removed line that
    itself starts with '-- ' from being read as a file header. Anything that is not
    exactly the unified format _record_changes writes returns None.
    """
    if not text.endswith("\n"):
        return None
    lines = _diff_lines(text)
    sections, index = [], 0
    while index < len(lines):
        start = index
        if not (index + 1 < len(lines) and lines[index].startswith("--- ")
                and lines[index + 1].startswith("+++ ")):
            return None
        name = lines[index + 1 if lines[index + 1] != "+++ /dev/null\n" else index][4:-1]
        path = name[2:] if name.startswith(("a/", "b/")) else name
        index += 2
        while index < len(lines) and lines[index].startswith("@@"):
            match = _HUNK_HEADER.match(lines[index])
            if match is None:
                return None
            old_left = 1 if match.group(1) is None else int(match.group(1))
            new_left = 1 if match.group(2) is None else int(match.group(2))
            index += 1
            while old_left or new_left:
                if index >= len(lines):
                    return None
                marker = lines[index][:1]
                if marker == " " and old_left and new_left:
                    old_left, new_left = old_left - 1, new_left - 1
                elif marker == "-" and old_left:
                    old_left -= 1
                elif marker == "+" and new_left:
                    new_left -= 1
                elif marker != "\\":
                    return None
                index += 1
            while index < len(lines) and lines[index].startswith("\\"):
                index += 1
        if index == start + 2:
            return None
        sections.append((path, "".join(lines[start:index])))
    return sections or None


def _cut_section(section: str, limit: int, *, parsed: bool = True) -> str | None:
    """Longest prefix of whole hunks, or of the first hunk's lines, within limit.

    Unparsed text has no file header or hunks and is cut at a line boundary instead.
    """
    if not parsed:
        kept, cost = [], 0
        for line in section.splitlines(keepends=True):
            cost += _json_cost(line)
            if cost > limit:
                break
            kept.append(line)
        return "".join(kept) or None
    lines = _diff_lines(section)
    starts = [i for i in range(2, len(lines)) if lines[i].startswith("@@")] + [len(lines)]
    kept, cost = 2, _json_cost("".join(lines[:2]))
    if cost > limit:
        return None
    for begin, end in zip(starts, starts[1:]):
        size = _json_cost("".join(lines[begin:end]))
        if cost + size > limit:
            if kept == 2:
                for line in lines[begin:end]:
                    size = _json_cost(line)
                    if cost + size > limit:
                        break
                    kept, cost = kept + 1, cost + size
            break
        kept, cost = end, cost + size
    return "".join(lines[:kept]) if kept > 2 else None


def _inline_diffs(rows: list[dict[str, Any]], budget: int) -> None:
    """Inline member diffs fairly: every member's implementation before any tests.

    Each phase (implementation files, then test files) gives members an even share of
    the remaining budget in ascending order of need, so slack from small diffs flows to
    larger ones and one member's tests never displace another's implementation. Within a
    share, files go whole smallest first; one that does not fit is cut to a hunk-boundary
    prefix when more than 2 KiB of the share remains, else omitted. A truncated member
    names each partial/omitted file in diff_inline_files; diff_path keeps the full diff.
    """
    members = []
    for row in rows:
        changes = row.get("changes")
        if not (isinstance(changes, dict) and changes.get("status") == "COMPLETE"
                and changes.get("diff_bytes")):
            continue
        text = _diff_text(changes)
        if text is None:
            continue
        parsed = _diff_sections(text)
        sections = [{"path": path, "text": part, "test": _is_test_path(path),
                     "cost": _json_cost(part), "inline": None}
                    for path, part in (parsed or [(None, text)])]
        manifest = [{"inline": "omitted", "path": section["path"]} for section in sections] if parsed else []
        # Quotes plus a worst-case manifest are charged before any diff text.
        budget -= 2 + (len(json.dumps({"diff_inline_files": manifest}, ensure_ascii=True,
                                      sort_keys=True)) + 2 if manifest else 0)
        members.append({"row": row, "sections": sections, "parsed": parsed is not None})
    for test_phase in (False, True):
        needs = sorted(((sum(s["cost"] for s in m["sections"] if s["test"] == test_phase), m)
                        for m in members), key=lambda item: item[0])
        needs = [member for need, member in needs if need]
        for position, member in enumerate(needs):
            share, used = max(0, budget) // (len(needs) - position), 0
            phase = [s for s in member["sections"] if s["test"] == test_phase]
            for section in sorted(phase, key=lambda s: s["cost"]):
                left = share - used
                if section["cost"] <= left:
                    section["inline"], used = section["text"], used + section["cost"]
                elif left > 2048:
                    cut = _cut_section(section["text"], left, parsed=member["parsed"])
                    if cut is not None:
                        section["inline"], used = cut, used + _json_cost(cut)
            budget -= used
    for member in members:
        changes = dict(member["row"]["changes"])
        kept = [s["inline"] for s in member["sections"] if s["inline"] is not None]
        short = [s for s in member["sections"] if s["inline"] != s["text"]]
        if kept:
            changes["diff"] = "".join(kept)
        changes["diff_inline_truncated"] = bool(short)
        if short and member["parsed"]:
            changes["diff_inline_files"] = [
                {"inline": "omitted" if s["inline"] is None else "partial", "path": s["path"]} for s in short]
        member["row"]["changes"] = changes


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
    if member.artifact_root_blocker is not None:
        return _row(member, blocker=member.artifact_root_blocker)
    run = member.artifact_root / member.request_id
    try:
        if caller._plain_path(member.artifact_root, name="artifact_root", directory=True) != member.artifact_root:
            return _row(member, blocker="NOKIY_BATCH_ARTIFACT_ROOT_UNAVAILABLE")
        if run.is_symlink() or (run.exists() and not run.is_dir()):
            return _row(member, blocker="NOKIY_EMBEDDED_UNCERTAIN_PRIOR_ATTEMPT")
        if not run.exists():
            return _row(member, blocker="NOKIY_BATCH_NOT_STARTED")
        if not os.path.lexists(run / "terminal.json"):
            caller.await_live_execution(run)  # a member still running seals its own terminal
        terminal = _project(member, caller.read_terminal(member.artifact_root, member.request_id))
        return _row(member, terminal={**terminal, "inspection_summary": None,
                                     "inspection_summary_omission": "READ_ONLY_RECOVERY"})
    except (caller.EmbeddedNokiyError, OSError, ValueError, RuntimeError) as error:
        return _row(member, blocker=getattr(error, "code", "NOKIY_BATCH_READ_UNCERTAIN"))


def read_batch(path: Path, sha256: str) -> dict[str, Any]:
    verifiers: list = []
    members, _ = _load_plan(path, sha256, recovery=True, post_batch_verifiers=verifiers)
    result = _aggregate(members, _read_rows(members))
    if verifiers:
        from .post_batch_verifier import recover
        result = recover(verifiers[0], sha256, result)
    return result


def _read_rows(members: tuple[Member, ...]) -> dict[str, dict[str, Any]]:
    rows = {m.request_id: _read(m) for m in members}
    for member in members:
        changes = _load_changes(member)
        if changes is not None:
            rows[member.request_id]["changes"] = changes
    return rows


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


def _drop_postimage_context(rows: list[dict[str, Any]]) -> list[tuple[dict, dict]]:
    dropped = []
    for row in rows:
        for receipt in row.get("verifier_delivery", {}).get("receipts", []):
            if "source_postimage_context" in receipt:
                dropped.append((receipt, receipt.pop("source_postimage_context")))
    return dropped


def _fit_summary(summary: dict[str, Any], *, max_bytes: int = 8000) -> dict[str, Any]:
    """Maintain exact JSON byte accounting while applying the legacy truncation order."""
    def encoded(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("ascii")

    rows = summary["members"]
    for row in rows:
        row["result_truncated"] = bool(row["result_truncated"])
    size = len(encoded(summary))
    if size > 8000:
        compact = _compact_summary(summary)
        compact_size = len(encoded(compact))
        if compact_size < size:
            summary, size = compact, compact_size
            rows = summary["members"]
    if size > max_bytes and _drop_postimage_context(rows):
        size = len(encoded(summary))
    if size > max_bytes and any("result_artifact_preview" in row for row in rows):
        # Optional report transport must not displace legacy text or artifact references.
        for row in rows:
            row.pop("result_artifact_preview", None)
        size = len(encoded(summary))
    if size > max_bytes and any("verifier_delivery" in row for row in rows):
        # Optional receipt transport must not truncate the existing result.
        for row in rows:
            row.pop("verifier_delivery", None)
        size = len(encoded(summary))
    # JSON structure and metadata are now fixed. Only one string and its bool
    # change per iteration: do not encode all other worker results again.
    text_sizes = [len(encoded(row["result_text"])) if row["result_text"] else 0
                  for row in rows] if size > max_bytes else []
    while size > max_bytes and any(text_sizes):
        index = max(range(len(rows)), key=text_sizes.__getitem__)
        largest = rows[index]
        text = largest["result_text"]
        replacement = text[:max(0, len(text) // 2)]
        replacement_size = len(encoded(replacement))
        size += replacement_size - text_sizes[index]
        if not largest["result_truncated"]:
            size -= 1  # The JSON token false (5 bytes) becomes true (4 bytes).
        largest["result_text"] = replacement
        largest["result_truncated"] = True
        text_sizes[index] = replacement_size if replacement else 0
    if size > max_bytes:
        return {"schema_version": "nokiy_direct_batch_summary_v1",
                "status": "COMPACT_METADATA_TOO_LARGE",
                "first_typed_blocker": "NOKIY_BATCH_SUMMARY_TOO_LARGE",
                "recovery": "Use the printed original plan path and SHA with read-batch; use read-result for original terminals."}
    return summary


# Failed command records an inspection may explain: authenticated source-read
# non-attempts, searches whose exit 1 means nothing matched, and effect-free reads
# whose expected digest did not match the file, and closed inspect-shell exits.
_RECONCILIATIONS = (("source_read_rejection_reconciliation", "nokiy_source_read_rejection_reconciliation_v1",
                     "not_attempted_count"),
                    ("search_no_match_reconciliation", "nokiy_search_no_match_reconciliation_v1",
                     "no_match_count"),
                    ("source_read_digest_mismatch_reconciliation",
                     "nokiy_source_read_digest_mismatch_reconciliation_v1", "mismatch_count"),
                    ("inspect_shell_failure_reconciliation",
                     "nokiy_inspect_shell_failure_reconciliation_v1", "failure_count"),
                    ("source_read_no_content_reconciliation",
                     "nokiy_source_read_no_content_reconciliation_v1", "failure_count"),
                    ("jspace_refusal_reconciliation",
                     "nokiy_jspace_refusal_reconciliation_v1", "not_attempted_count"))


def _reconciled_failures(inspection: dict[str, Any]) -> dict[str, int] | None:
    """How many failed records each present reconciliation explains; None if any is malformed."""
    explained = {}
    for key, schema, field in _RECONCILIATIONS:
        value = inspection.get(key)
        if value is None:
            continue
        if (not isinstance(value, dict)
                or set(value) != {"schema_version", field, "unresolved_count", "effective_command_success"}
                or value["schema_version"] != schema
                or type(value[field]) is not int or not 0 < value[field] < 2**53
                or type(value["unresolved_count"]) is not int or value["unresolved_count"] != 0
                or value["effective_command_success"] != "known_success"):
            return None
        # Several reconciliations share a count field; their explained failures add up.
        explained[field] = explained.get(field, 0) + value[field]
    return explained


def _delivery_binding(terminal: dict[str, Any], inspection: dict[str, Any],
                      text: Any, verified_answer: bool) -> dict[str, Any] | None:
    """Bind only a complete, inspected read-only answer; never accept its meaning."""
    counts = inspection.get("counts") or {}
    if not isinstance(counts, dict):
        return None
    failed = counts.get("failed")
    explained = _reconciled_failures(inspection)
    reconciled = (type(failed) is int and 0 < failed < 2**53
                  and inspection.get("command_success") == "known_failure"
                  and explained is not None and sum(explained.values()) == failed)
    commands_settled = reconciled or (type(failed) is int and failed == 0
                                      and inspection.get("command_success") == "known_success")
    if (terminal.get("status") != "RESULT_AVAILABLE" or terminal.get("cleanup_pass") is not True
            or type(terminal.get("runtime_exit_code")) is not int or terminal.get("runtime_exit_code") != 0
            or terminal.get("fallback_used") is not False
            or terminal.get("capability_gap") is not None or terminal.get("first_typed_blocker") is not None
            or terminal.get("first_blocker") is not None
            or terminal.get("mission_acceptance") != "parent_owned" or terminal.get("continuation_owner") != "codex"
            or inspection.get("status") != "EVIDENCE_VERIFIED" or inspection.get("first_blocker") is not None
            or inspection.get("projection_partial") is True or inspection.get("artifact_integrity") != "verified"
            or not commands_settled
            or type(counts.get("file_changes")) is not int or counts["file_changes"] != 0
            or counts.get("index_complete") is not True
            or (not verified_answer and terminal.get("result_truncated") is not False)
            or not isinstance(text, str) or not text):
        return None
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    digest = hashlib.sha256(encoded).hexdigest()
    artifact = terminal.get("result_artifact")
    if artifact is not None and (not isinstance(artifact, dict)
            or artifact.get("sha256") != digest or artifact.get("bytes") != len(encoded)):
        return None
    if not reconciled:
        reconciliation = {"schema_version": "nokiy_batch_delivery_binding_v1"}
    elif "no_match_count" not in explained:
        reconciliation = {"schema_version": "nokiy_batch_delivery_binding_v2", "not_attempted_count": failed}
    else:
        reconciliation = {"schema_version": "nokiy_batch_delivery_binding_v3",
                          "not_attempted_count": explained.get("not_attempted_count", 0),
                          "no_match_count": explained["no_match_count"]}
    return {**reconciliation, "result_sha256": digest,
            "result_bytes": len(encoded), "terminal_sha256": inspection.get("terminal_sha256")}


def _verified_postimage_context(inspection: dict[str, Any], sources: dict) -> bool:
    """Reuse inspected context only; no extraction, source reads or new authority."""
    schemas = {
        postimage_context.SCHEMA_VERSION: ("verifier_postimage_context", postimage_context.NOTICE),
        postimage_context.DELTA_SCHEMA_VERSION: ("verifier_postimage_delta", postimage_context.DELTA_NOTICE),
        postimage_context.DELTA_NAVIGATION_SCHEMA_VERSION: (
            "verifier_postimage_delta", postimage_context.DELTA_NAVIGATION_NOTICE),
    }
    schema = sources.get("schema_version")
    files = sources.get("files")
    if (inspection.get("projection_partial") is True or not isinstance(schema, str) or schema not in schemas
            or (sources.get("kind"), sources.get("notice")) != schemas[schema]
            or not isinstance(files, list) or not files):
        return False
    changes = inspection.get("file_changes")
    postimages = changes.get("postimages") if isinstance(changes, dict) else None
    if not isinstance(postimages, list):
        return False
    current = {row.get("path"): row.get("sha256") for row in postimages if isinstance(row, dict)}
    for file in files:
        if (not isinstance(file, dict) or not isinstance(file.get("path"), str)
                or not isinstance(file.get("postimage_sha256"), str)
                or file["postimage_sha256"] != current.get(file.get("path"))):
            return False
        if schema == postimage_context.SCHEMA_VERSION:
            locators = file.get("locators")
            if (not isinstance(locators, list) or not locators or not file.get("spans")
                    or any(not isinstance(row, dict) or row.get("complete") is not True for row in locators)):
                return False
        elif file.get("coverage") != "all_text_changes" or not file.get("hunks"):
            return False
    return True


def _verifier_delivery(inspection: dict[str, Any]) -> dict[str, Any] | None:
    """Project existing matched verifier receipts, not a new acceptance claim."""
    if (inspection.get("status") != "EVIDENCE_VERIFIED"
            or inspection.get("artifact_integrity") != "verified"
            or inspection.get("first_blocker") is not None):
        return None
    context = inspection.get("parent_review_context") or {}
    if not isinstance(context, dict):
        return None
    commands = inspection.get("commands") or []
    if not isinstance(commands, list):
        return None
    matched = {c.get("event_index") for c in commands
               if isinstance(c, dict) and type(c.get("event_index")) is int
               and c.get("execution_proof") == "known_success"
               and c.get("receipt_check") == "matched"
               and type(c.get("exit_code")) is int and c["exit_code"] == 0}
    receipts, delivered_contexts = [], set()
    contexts = context.get("verifier_contexts", [])
    if not isinstance(contexts, list):
        return None
    for item in contexts:
        if not isinstance(item, dict):
            continue
        event = item.get("source_event") or {}
        sources = item.get("source_postimages") or {}
        if (not isinstance(event, dict) or not isinstance(sources, dict)
                or type(event.get("event_index")) is not int
                or event["event_index"] not in matched):
            continue
        receipts.append({"source_event": event,
                         "source_postimages": [
                             {key: file[key] for key in
                              ("path", "preimage_sha256", "postimage_sha256") if key in file}
                             for file in sources.get("files", []) if isinstance(file, dict)]})
        if _verified_postimage_context(inspection, sources):
            identity = json.dumps(sources, sort_keys=True, ensure_ascii=True)
            if identity not in delivered_contexts:
                receipts[-1]["source_postimage_context"] = sources
                delivered_contexts.add(identity)
    represented = {r["source_event"]["event_index"] for r in receipts}
    for command in commands:
        if (not isinstance(command, dict) or type(command.get("event_index")) is not int
                or command["event_index"] not in matched
                or command["event_index"] in represented):
            continue
        try:
            locator = json.loads(command.get("command") or "null")
        except (ValueError, TypeError):
            continue
        if (not isinstance(locator, dict) or set(locator) != {"verifier_index"}
                or type(locator["verifier_index"]) is not int or locator["verifier_index"] < 0):
            continue
        receipts.append({"source_event": {"event_index": command["event_index"],
                                          "verifier_index": locator["verifier_index"],
                                          "terminal_sha256": inspection.get("terminal_sha256")},
                         "execution_proof": "known_success", "receipt_check": "matched",
                         "exit_code": 0, "source_postimages": [],
                         "source_context_available": False})
    if not receipts:
        return None
    return {"schema_version": "nokiy_batch_verifier_delivery_v1",
            "scope": "recorded_successful_verifier_receipts_only",
            "parent_acceptance_required": True, "live_source_freshness": False,
            "dependency_coverage": False, "receipts": receipts,
            "pagination": inspection.get("pagination")}


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """Opt-in CLI projection; raw terminals remain available via read-result."""
    rows = []
    for row in result["members"]:
        terminal = row.get("terminal") or {}
        inspection = terminal.get("result_inspection") or {}
        text = terminal.get("result_text")
        answer = inspection.get("parent_review_answer")
        # project_terminal already bound this answer to the original terminal.
        # Deliver it once instead of discarding it and forcing another tool read.
        verified_answer = (row["status"] == "RESULT_AVAILABLE" and row["cleanup_pass"] is True
                           and inspection.get("status") == "EVIDENCE_VERIFIED"
                           and isinstance(answer, dict)
                           and answer.get("schema_version") == "nokiy_parent_review_answer_v1"
                           and answer.get("parent_acceptance_required") is True
                           and answer.get("artifact") == terminal.get("result_artifact")
                           and isinstance(answer.get("text"), str))
        if verified_answer:
            text = answer["text"]
        delivery = _delivery_binding(terminal, inspection, text, verified_answer)
        verifier_delivery = _verifier_delivery(inspection)
        recovery_observations = [
            {"event_index": command.get("event_index"),
             "observation": command["verifier_recovery_observation"]}
            for command in inspection.get("commands", [])
            if isinstance(command, dict)
            and isinstance(command.get("verifier_recovery_observation"), dict)
        ]
        rows.append({
            "request_id": row["request_id"], "artifact_root": row["artifact_root"],
            "request_sha256": terminal.get("request_sha256"),
            "native_thread_id": terminal.get("native_thread_id"),
            "status": row["status"], "first_typed_blocker": row["first_typed_blocker"],
            "cleanup_pass": row["cleanup_pass"],
            "model": terminal.get("model"),
            "observed_model": terminal.get("observed_model"),
            "model_route": terminal.get("model_route"),
            "reasoning_effort": terminal.get("reasoning_effort"),
            "requested_service_tier": terminal.get("requested_service_tier"),
            "observed_service_tier": terminal.get("observed_service_tier"),
            **{key: terminal[key] for key in ("requested_terminal_delivery",
                                              "observed_terminal_delivery", "terminal_evidence")
               if key in terminal},
            "usage": terminal.get("usage"),
            "wall_time_seconds": terminal.get("wall_time_seconds"),
            "result_text": text if isinstance(text, str) else None,
            "result_truncated": False if verified_answer else terminal.get("result_truncated", False),
            **({"result_text_source": "verified_final_artifact",
                "terminal_result_truncated": terminal.get("result_truncated", False)}
               if verified_answer else {}),
            "result_artifact": terminal.get("result_artifact"),
            "evidence_status": inspection.get("status"),
            "evidence_counts": inspection.get("counts"),
            "evidence_first_blocker": inspection.get("first_blocker"),
            **({"verifier_delivery": verifier_delivery} if verifier_delivery else {}),
            **({"verifier_failure_resolution": inspection["verifier_failure_resolution"]}
               if isinstance(inspection.get("verifier_failure_resolution"), dict) else {}),
            **({"post_batch_verifier_settlement": inspection["post_batch_verifier_settlement"]}
               if isinstance(inspection.get("post_batch_verifier_settlement"), dict) else {}),
            **({"verifier_recovery_observations": {
                "commands": recovery_observations,
                "pagination": inspection.get("pagination"),
                "parent_acceptance_required": True,
                "retry_authorized": False,
            }} if recovery_observations else {}),
            "source_read_efficiency": inspection.get("source_read_efficiency"),
            "terminal_path": str(Path(row["artifact_root"]) / row["request_id"] / "terminal.json"),
            "terminal_sha256": inspection.get("terminal_sha256"),
            **({"delivery_binding": delivery} if delivery is not None else {}),
            **{key: terminal[key] for key in ("result_artifact_preview", "inspection_summary",
                                              "inspection_summary_diagnostic", "inspection_summary_omission")
               if key in terminal},
            **({"changes": row["changes"]} if isinstance(row.get("changes"), dict) else {}),
        })
    summary = {"schema_version": "nokiy_direct_batch_summary_v1",
               "status": result["status"],
               "first_typed_blocker": result["first_typed_blocker"],
               "members": rows, "continuation_owner": "codex",
               "mission_acceptance": "parent_owned"}
    if "post_batch_verifier" in result:
        summary["post_batch_verifier"] = result["post_batch_verifier"]
    if not any("changes" in row for row in rows):
        return _fit_summary(summary, max_bytes=MAX_PROJECTED_BYTES)
    # Mutation batches: deliver each member's exact diff so the parent reviews it
    # directly; the full diff and its record stay in the member artifact root.
    base = len(json.dumps(summary, ensure_ascii=True, sort_keys=True).encode("ascii"))
    contexts = (_drop_postimage_context(rows)
                if base > MAX_BATCH_SUMMARY_BYTES - MAX_INLINE_DIFF_BYTES - 4096 else [])
    if contexts:
        base = len(json.dumps(summary, ensure_ascii=True, sort_keys=True).encode("ascii"))
    if base > MAX_BATCH_SUMMARY_BYTES - MAX_INLINE_DIFF_BYTES - 4096:
        for row in rows:
            row.pop("verifier_delivery", None)
        base = len(json.dumps(summary, ensure_ascii=True, sort_keys=True).encode("ascii"))
    plain = json.loads(json.dumps(summary))
    _inline_diffs(rows, min(MAX_INLINE_DIFF_BYTES, MAX_BATCH_SUMMARY_BYTES - base - 4096))
    # Reserve the original diff share first; context may use only actual spare space.
    # Receipts removed by the legacy fallback are no longer part of the summary.
    for receipt, context in contexts:
        receipt["source_postimage_context"] = context
    fitted = _fit_summary(summary, max_bytes=MAX_BATCH_SUMMARY_BYTES)
    if fitted.get("status") == "COMPACT_METADATA_TOO_LARGE":
        fitted = _fit_summary(plain, max_bytes=MAX_BATCH_SUMMARY_BYTES)
    return fitted


def run_batch(path: Path, sha256: str) -> dict[str, Any]:
    verifiers: list = []
    members, parallel = _load_plan(path, sha256, post_batch_verifiers=verifiers)
    verifier = verifiers[0] if verifiers else None
    if verifier is not None:
        from . import post_batch_verifier as post

    def recovered() -> dict[str, Any]:
        result = _aggregate(members, _read_rows(members))
        return post.recover(verifier, sha256, result) if verifier is not None else result
    caller_thread = os.environ.get("CODEX_THREAD_ID")
    if not isinstance(caller_thread, str) or not caller.THREAD_ID_PATTERN.fullmatch(caller_thread):
        _fail("actual native thread ID unavailable")
    # Any existing run (including a successful terminal) makes this invocation
    # result-only. Never infer that a missing peer is safe to resend on reentry.
    if (verifier is not None and post.exists(verifier)) or any(
            (m.artifact_root / m.request_id).exists() or
            (m.artifact_root / m.request_id).is_symlink() for m in members):
        return recovered()
    capacity = worker_capacity()
    if len(members) > capacity:
        _fail(f"batch has {len(members)} members but configured worker capacity is {capacity}")
    requests = []
    scopes = []
    workspaces = set()
    # Preflight is read-only and provider-free. Overlap its independent I/O,
    # but preserve member-order errors and the all-ready barrier before effects.
    pending_error = None
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = []
        for member in members:
            try:
                request, raw = load_prepared_request(member.path)
                if (hashlib.sha256(raw).hexdigest() != member.file_sha256
                        or request.request_id != member.request_id
                        or request.artifact_root != member.artifact_root
                        or request.schema_version != caller.REQUEST_SCHEMA_VERSION
                        or request.execution_profile != "direct"
                        or request.native_thread_id != caller_thread):
                    _fail("prepared member identity, thread or profile differs")
                workspaces.add(request.workspace)
                if _overlap({member.artifact_root}, {request.workspace}):
                    _fail("artifact root and workspace overlap")
                scopes.append(_scopes(request))
            except (caller.EmbeddedNokiyError, OSError, ValueError) as error:
                pending_error = error
                break
            futures.append(pool.submit(copy_context().run, caller.preflight, request))
            requests.append(request)
        for future in futures:
            ready = future.result()
            if ready.get("status") != "READY":
                _fail("member preflight is not READY")
        if pending_error is not None:
            raise pending_error
    # Members may use different workspaces. Every artifact root stays outside all of them, and
    # the absolute-path overlap checks below also cover nested workspaces.
    if any(_overlap({member.artifact_root}, workspaces) for member in members):
        _fail("artifact root and workspace overlap")
    for index, (reads, writes) in enumerate(scopes):
        for other_index, (other_reads, other_writes) in enumerate(scopes[index + 1:], start=index + 1):
            _check_discovery_aliases(reads, other_writes)
            _check_discovery_aliases(other_reads, writes)
            if (_overlap(writes, {members[other_index].artifact_root})
                    or _overlap(other_writes, {members[index].artifact_root})):
                _fail("foreign member artifact ownership overlap")
            if (_overlap(writes, other_reads) or _overlap(writes, other_writes)
                    or _overlap(other_writes, reads)):
                _fail("cross-member read/write or write/write overlap")
    # Preflight is not a lease: a run appearing during validation is not retried.
    if any((m.artifact_root / m.request_id).exists() or
           (m.artifact_root / m.request_id).is_symlink() for m in members):
        return recovered()

    if len(members) > worker_capacity():
        _fail("batch exceeds current configured worker capacity")

    gate = post.prepare(verifier, path, sha256, members, requests, scopes) if verifier is not None else None
    if gate is not None and not post.claim(gate, path):
        return recovered()

    preimages: dict[str, dict[str, bytes | None] | None] = {}

    def execute_one(member: Member, request: caller.EmbeddedNokiyRequest, scope) -> dict[str, Any]:
        try:
            # Acquire before preparing a member's engine deadline, not after
            # spending its execution budget waiting for a parallel slot.
            with (scope.member(member.request_id, cancelled=lambda: bool(cancelled))
                  if scope is not None else nullcontext()):
                preimages[member.request_id] = _capture_preimages(request)
                terminal = caller.execute(request)
            return _row(member, terminal=terminal)
        except Exception as error:
            # A crash after create-only claim without a terminal is uncertain.
            return _row(member, blocker=getattr(error, "code", "NOKIY_BATCH_EXECUTION_UNCERTAIN"))

    # Share cancellation with every worker in this invocation, not other runs.
    with caller._cancellation_signals() as cancelled:
        with native_batch_scope([m.request_id for m in members], parallel) as scope:
            # Native members wait for shared cleanup after releasing their engine
            # slot; give every admitted member a thread to avoid barrier deadlock.
            threads = len(members) if scope is not None else parallel
            with ThreadPoolExecutor(max_workers=threads) as pool:
                try:
                    futures = [pool.submit(copy_context().run, execute_one, member, request, scope)
                               for member, request in zip(members, requests)]
                except BaseException:
                    if scope is not None:
                        scope.abort()
                    raise
                rows = {member.request_id: future.result() for member, future in zip(members, futures)}
    # The native root can prove its scope is empty only after the execution pool
    # has joined. Do not inspect/publish root settlement from a worker thread.
    for member, request in zip(members, requests):
        terminal = rows[member.request_id]["terminal"]
        if terminal is not None:
            try:
                terminal = _project(member, terminal)
                terminal = retain_inspection_summary(terminal, member.artifact_root, member.request_id,
                                                     expected_thread_id=request.native_thread_id)
                rows[member.request_id] = _row(member, terminal=terminal)
            except Exception as error:
                rows[member.request_id] = _row(member, blocker=getattr(
                    error, "code", "NOKIY_BATCH_EXECUTION_UNCERTAIN"))
        if member.request_id in preimages:
            changes = _record_changes(member, request, preimages[member.request_id])
            if changes is not None:
                rows[member.request_id]["changes"] = changes
    result = _aggregate(members, rows)
    return post.finish(gate, result, cancelled=bool(cancelled)) if gate is not None else result
