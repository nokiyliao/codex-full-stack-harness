# SPDX-License-Identifier: MIT
"""Read sealed call evidence and prepare a lossless task-retirement snapshot.

This module does not delete, move, restore, unlock, or persist anything. A
Native owner must separately prove terminal task state and adopt a replay-safe
retirement transaction before reclaiming any of the originals.
"""

from __future__ import annotations

import base64
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import canonical_sha256
from .graph_entry import MAX_TASK_BYTES, MAX_TASK_FILES, GraphEntryError, _encoded, _IDENTITY, _object

_KEY = re.compile(r"[0-9a-f]{64}\.json(?:\.gz)?\Z")
_DIRECTORIES = {".tura", ".tura/run", ".tura/run/command_receipts"}


@dataclass(frozen=True)
class RetirementCandidate:
    report: dict[str, Any]
    archive: bytes


def _decode(data: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise GraphEntryError("GRAPH_RETENTION_NONFINITE_JSON")

    value = json.loads(data, object_pairs_hook=_object, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise GraphEntryError("GRAPH_RETENTION_OBJECT_REQUIRED")
    return value


def _inflate(data: bytes, maximum: int) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise GraphEntryError("GRAPH_RETENTION_DECOMPRESSION_LIMIT")
    return raw


def _safe_id(value: str) -> str:
    return "".join(c if c.isascii() and (c.isalnum() or c in "-_.")
                   else f"_x{ord(c):x}_" for c in value)


def _validate_pair(
    root: Path, task_id: str, key: str, files: dict[str, bytes], remaining: int,
) -> tuple[int, dict[str, Any]]:
    guard = _decode(files[key + ".json"])
    raw_bundle = _inflate(files[key + ".json.gz"], remaining)
    bundle = _decode(raw_bundle)
    if (bundle.get("schema_version") != "codex_tura_graph_terminal_bundle_v1"
            or bundle.get("semantic_sha256") != canonical_sha256({
                k: v for k, v in bundle.items() if k != "semantic_sha256"})):
        raise GraphEntryError("GRAPH_RETENTION_BUNDLE_DIGEST_MISMATCH")
    members = bundle.get("members")
    if not isinstance(members, list) or not 4 <= len(members) <= 42:
        raise GraphEntryError("GRAPH_RETENTION_MEMBERS_INVALID")
    decoded = []
    paths = []
    for member in members:
        if not isinstance(member, dict) or set(member) != {"path", "bytes", "sha256", "content"}:
            raise GraphEntryError("GRAPH_RETENTION_MEMBER_INVALID")
        path, content = member["path"], member["content"]
        if (not isinstance(path, str) or not path or not isinstance(content, str)
                or Path(path).is_absolute() or ".." in Path(path).parts or path in paths):
            raise GraphEntryError("GRAPH_RETENTION_MEMBER_PATH_INVALID")
        raw = content.encode()
        if (type(member["bytes"]) is not int or len(raw) != member["bytes"]
                or hashlib.sha256(raw).hexdigest() != member["sha256"]):
            raise GraphEntryError("GRAPH_RETENTION_MEMBER_DIGEST_MISMATCH")
        paths.append(path)
        decoded.append(_decode(raw))
    journal, batch = decoded[:2]
    call_id, result = journal.get("call_id"), journal.get("result")
    if (not isinstance(call_id, str) or not _IDENTITY.fullmatch(call_id)
            or canonical_sha256([task_id, call_id]) != key
            or journal.get("task_id") != task_id
            or journal.get("schema_version") != "codex_tura_graph_journal_v1"
            or type(journal.get("closeout_version")) is not int
            or journal["closeout_version"] != 1 or journal.get("status") != "completed"
            or not isinstance(result, dict) or result.get("status") != "completed"
            or result.get("task_id") != task_id or result.get("call_id") != call_id
            or result.get("request_digest") != journal.get("request_digest")
            or not isinstance(journal.get("request_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", journal["request_digest"])
            or journal.get("result_digest") != canonical_sha256(result)):
        raise GraphEntryError("GRAPH_RETENTION_TERMINAL_IDENTITY_INVALID")
    scope = journal.get("process_scope", {})
    if (type(journal.get("supervision_version")) is not int or journal["supervision_version"] != 1
            or not isinstance(scope, dict)
            or scope.get("scope") != "per-call-inherited-seatbelt-signal-boundary"
            or scope.get("engine_reaped") is not True
            or scope.get("no_live_descendants") is not True
            or scope.get("cleanup_error", "missing") is not None
            or type(scope.get("engine_exit_code")) is not int or scope["engine_exit_code"] != 0
            or type(scope.get("descendant_signals")) is not int or scope["descendant_signals"] != 0
            or scope.get("effect_outcome_inferred_from_process_exit") is not False
            or result.get("process_closeout_required") is not False):
        raise GraphEntryError("GRAPH_RETENTION_PROCESS_CLOSEOUT_UNPROVEN")
    expected_guard = {
        "schema_version": "codex_tura_graph_journal_v1", "closeout_version": 1,
        "task_id": task_id, "call_id": call_id, "request_digest": journal["request_digest"],
        "status": "sealed", "archive_semantic_sha256": bundle["semantic_sha256"],
        "recovery_artifact": {"format": "gzip-terminal-bundle-v1",
                              "path": str(root / (key + ".json.gz"))},
    }
    if (_encoded(guard) != _encoded(expected_guard)
            or result.get("recovery_artifact") != expected_guard["recovery_artifact"]):
        raise GraphEntryError("GRAPH_RETENTION_ROLLBACK_GUARD_MISMATCH")
    ids = batch.get("call_ids")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 20
            or any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids) or len(members) != 2 + 2 * len(ids)
            or batch.get("schema_version") != "tura_command_run_batch_admission_v1"
            or batch.get("execution_id") != f"{task_id}:{call_id}"
            or batch.get("state") != "finished"
            or not isinstance(batch.get("accepted_call_ids"), list)
            or any(not isinstance(i, str) for i in batch["accepted_call_ids"])
            or sorted(batch["accepted_call_ids"]) != sorted(ids)):
        raise GraphEntryError("GRAPH_RETENTION_BATCH_NOT_TERMINAL")
    prefix = ".tura/run/command_receipts/"
    expected_paths = [key + ".json", prefix + _safe_id(f"{task_id}:{call_id}") + ".batch-admission"]
    for index, node_id in enumerate(ids):
        expected_paths.extend([prefix + _safe_id(node_id) + suffix for suffix in (".claim.json", ".json")])
        claim, receipt = decoded[2 + 2 * index:4 + 2 * index]
        if (claim.get("schema_version") != "tura_command_execution_claim_v1"
                or receipt.get("schema_version") != "tura_command_terminal_receipt_v1"
                or claim.get("call_id") != node_id or receipt.get("call_id") != node_id
                or claim.get("state") != "completed" or receipt.get("terminal_state") != "completed"
                or receipt.get("outcome") != "known"
                or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0
                or receipt.get("termination_proven") is not True
                or journal.get("nodes", {}).get(node_id, {}).get("status") != "completed"
                or any(v.get("process_reaped") is not True or v.get("process_group_empty") is not True
                       or v.get("reconcile_required") is not False for v in (claim, receipt))):
            raise GraphEntryError("GRAPH_RETENTION_NODE_UNSETTLED")
    if paths != expected_paths:
        raise GraphEntryError("GRAPH_RETENTION_MEMBER_PATH_MISMATCH")
    return len(raw_bundle), result


def _read(fd: int, name: str, remaining: int) -> tuple[bytes, int]:
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(child, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise GraphEntryError("GRAPH_RETENTION_FILE_IDENTITY_INVALID")
        if before.st_size > remaining:
            raise GraphEntryError("GRAPH_RETENTION_SNAPSHOT_LIMIT")
        data = stream.read(remaining + 1)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (any(getattr(before, field) != getattr(after, field) for field in fields)
                or len(data) != before.st_size or len(data) > remaining):
            raise GraphEntryError("GRAPH_RETENTION_SNAPSHOT_CHANGED")
        return data, stat.S_IMODE(before.st_mode)


def read_completed_call(
    artifact_root: Path, task_id: str, call_id: str, request_digest: str,
) -> dict[str, Any]:
    """Read one sealed outcome, not renew an execution or task authorization."""
    if (not isinstance(task_id, str) or not _IDENTITY.fullmatch(task_id)
            or not isinstance(call_id, str) or not _IDENTITY.fullmatch(call_id)
            or not isinstance(request_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", request_digest)):
        raise GraphEntryError("GRAPH_RESULT_IDENTITY_INVALID")
    parent = artifact_root.resolve(strict=True) / "tura-graph"
    root = parent / task_id
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        parent_fd = os.open(parent, flags)
        stack.callback(os.close, parent_fd)
        fd = os.open(task_id, flags, dir_fd=parent_fd)
        stack.callback(os.close, fd)
        # A sibling writer may remain active. Immutable exact-pair verification,
        # not acquisition of that writer's task lock, governs this read.
        key = canonical_sha256([task_id, call_id])
        names = [key + suffix for suffix in (".json", ".json.gz")]
        identity_fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")

        def snapshot() -> tuple[dict[str, bytes], dict[str, tuple[int, ...]]]:
            files, identities = {}, {}
            total = 0
            for name in names:
                try:
                    before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    files[name], _ = _read(fd, name, MAX_TASK_BYTES - total)
                    after = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise GraphEntryError("GRAPH_RESULT_SEALED_PAIR_MISSING") from exc
                identities[name] = tuple(getattr(before, field) for field in identity_fields)
                if identities[name] != tuple(getattr(after, field) for field in identity_fields):
                    raise GraphEntryError("GRAPH_RESULT_SNAPSHOT_CHANGED")
                total += len(files[name])
            return files, identities

        files, identities = snapshot()
        _, result = _validate_pair(root, task_id, key, files, MAX_TASK_BYTES)
        if result["request_digest"] != request_digest:
            raise GraphEntryError("GRAPH_RESULT_REQUEST_MISMATCH")
        if snapshot() != (files, identities):
            raise GraphEntryError("GRAPH_RESULT_SNAPSHOT_CHANGED")
        # Keep descriptors pinned through validation and reject namespace replacement.
        for path, descriptor in ((parent, parent_fd), (root, fd)):
            current, opened = path.lstat(), os.fstat(descriptor)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise GraphEntryError("GRAPH_RESULT_DIRECTORY_CHANGED")
        from .graph_entry import bounded_response, compact_result

        projected = compact_result(result, root)
        return bounded_response({
            **projected, "status": "retrieved" if projected.get("status") == "completed" else "blocked",
            "recorded_status": result["status"],
            "retrieval_only": True, "execution_started": False, "source_mutation_count": 0,
            "authority_effect": "none", "task_terminal_verified": False,
            "archive_sha256": hashlib.sha256(files[key + ".json.gz"]).hexdigest(),
            "guard_sha256": hashlib.sha256(files[key + ".json"]).hexdigest(),
        })


def prepare_task_retirement(artifact_root: Path, task_id: str) -> RetirementCandidate:
    """Snapshot only fully sealed calls under the existing owner's directory lock.

    The returned bytes are not persisted here. The report deliberately leaves
    Native task terminal/Goal/lease/reference state unverified. This function is
    not a cleanup admission, even when every call has completed.
    """
    if not isinstance(task_id, str) or not _IDENTITY.fullmatch(task_id):
        raise GraphEntryError("GRAPH_RETENTION_TASK_ID_INVALID")
    parent = artifact_root.resolve(strict=True) / "tura-graph"
    if parent.is_symlink():
        raise GraphEntryError("GRAPH_RETENTION_DIRECTORY_INVALID")
    root = parent / task_id
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(parent, flags)
    try:
        fd = os.open(task_id, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GraphEntryError("GRAPH_RETENTION_OWNER_BUSY") from exc
        identity = os.fstat(fd)
        files: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        directories: dict[str, int] = {}
        identities: dict[str, tuple[int, ...]] = {}
        total = count = 0

        def walk(current: int, relative: str = "") -> None:
            nonlocal total, count
            names = []
            with os.scandir(current) as entries:
                for entry in entries:
                    count += 1
                    if count > MAX_TASK_FILES:
                        raise GraphEntryError("GRAPH_RETENTION_SNAPSHOT_LIMIT")
                    names.append(entry.name)
            for name in sorted(names):
                path = relative + name
                info = os.stat(name, dir_fd=current, follow_symlinks=False)
                identities[path] = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if stat.S_ISDIR(info.st_mode) and path in _DIRECTORIES:
                    child = os.open(name, flags, dir_fd=current)
                    try:
                        directories[path] = stat.S_IMODE(info.st_mode)
                        walk(child, path + "/")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and not relative and _KEY.fullmatch(name):
                    files[path], modes[path] = _read(current, name, MAX_TASK_BYTES - total)
                    total += len(files[path])
                else:
                    raise GraphEntryError("GRAPH_RETENTION_UNSETTLED_OR_UNKNOWN_ENTRY")

        walk(fd)
        if not files:
            raise GraphEntryError("GRAPH_RETENTION_EMPTY_TASK")
        keys = sorted({name.split(".", 1)[0] for name in files})
        if set(files) != {key + suffix for key in keys for suffix in (".json", ".json.gz")}:
            raise GraphEntryError("GRAPH_RETENTION_INCOMPLETE_SEALED_PAIR")
        expanded = 0
        for key in keys:
            size, _ = _validate_pair(root, task_id, key, files, MAX_TASK_BYTES - expanded)
            expanded += size
        snapshot = {
            "schema_version": "codex_tura_graph_task_snapshot_v1", "task_id": task_id,
            "source_root": str(root), "root_mode": stat.S_IMODE(identity.st_mode),
            "directories": directories,
            "files": [{"path": name, "mode": modes[name], "bytes": len(data),
                       "sha256": hashlib.sha256(data).hexdigest(),
                       "content_base64": base64.b64encode(data).decode("ascii")}
                      for name, data in sorted(files.items())],
        }
        snapshot["semantic_sha256"] = canonical_sha256(snapshot)
        encoded = _encoded(snapshot)
        archive = gzip.compress(encoded, compresslevel=6, mtime=0)
        readback = _inflate(archive, len(encoded))
        if readback != encoded:
            raise GraphEntryError("GRAPH_RETENTION_PACK_READBACK_FAILED")
        restored = _decode(readback)
        reconstructed = {entry["path"]: base64.b64decode(entry["content_base64"], validate=True)
                         for entry in restored["files"]}
        if reconstructed != files:
            raise GraphEntryError("GRAPH_RETENTION_RECONSTRUCTION_FAILED")
        # Recheck the exact set and bytes while still holding the original lock.
        original_files, original_modes, original_dirs = files.copy(), modes.copy(), directories.copy()
        original_identities = identities.copy()
        files.clear()
        modes.clear()
        directories.clear()
        identities.clear()
        total = count = 0
        walk(fd)
        current = root.lstat()
        if (files != original_files or modes != original_modes or directories != original_dirs
                or identities != original_identities
                or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)
                or stat.S_IMODE(current.st_mode) != snapshot["root_mode"]
                or root.is_symlink() or parent.is_symlink()):
            raise GraphEntryError("GRAPH_RETENTION_SNAPSHOT_CHANGED")
        report = {
            "status": "prepared_not_admitted", "task_id": task_id, "source_root": str(root),
            "sealed_call_count": len(keys), "source_file_count": len(files),
            "source_directory_count": len(directories), "source_logical_bytes": total,
            "snapshot_semantic_sha256": snapshot["semantic_sha256"],
            "snapshot_sha256": hashlib.sha256(archive).hexdigest(), "snapshot_bytes": len(archive),
            "exact_byte_reconstruction_verified": True, "rollback_guards_preserved": True,
            "task_terminal_verified": False, "task_retirement_authorized": False,
            "first_typed_blocker": "GRAPH_TASK_TERMINAL_AUTHORITY_UNVERIFIED",
            "reclaimed_logical_bytes": 0, "source_mutation_count": 0,
            "persistent_artifacts_created": 0, "authority_effect": "none",
        }
        return RetirementCandidate(report, archive)
    finally:
        os.close(fd)
