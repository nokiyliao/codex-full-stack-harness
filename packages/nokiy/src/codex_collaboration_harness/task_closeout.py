"""Parent-bound, receipt-verified closeout of a single task lease."""
from __future__ import annotations

from pathlib import Path
import re
import stat
from typing import Any

from . import embedded_nokiy as caller

LIMIT = 128 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
TASK_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")


def fail(code: str) -> None:
    raise caller.EmbeddedNokiyError("NOKIY_DEPLOYMENT_" + code, code)


def leaf(path: Any) -> Path:
    if not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts:
        fail("CLOSEOUT_ABSOLUTE_PATH_REQUIRED")
    result = Path(path)
    if not result.parent.is_dir():
        fail("CLOSEOUT_PARENT_MISSING")
    try:
        if stat.S_ISLNK(result.lstat().st_mode):
            fail("CLOSEOUT_LEAF_SYMLINK")
    except FileNotFoundError:
        pass
    return result


def parent_identity(path: Path) -> dict[str, Any]:
    parent = path.parent.resolve(strict=True)
    info = parent.stat()
    return {"path": str(path), "parent_realpath": str(parent),
            "parent_device": info.st_dev, "parent_inode": info.st_ino}


def paths(spec: dict[str, Any]) -> dict[str, Any]:
    lease = leaf(spec["active_lease"]["path"])
    completion = leaf(spec["completion_path"])
    lease_pin = parent_identity(lease)
    completion_pin = parent_identity(completion)
    if (lease_pin["parent_realpath"], lease.name) == (completion_pin["parent_realpath"], completion.name):
        fail("CLOSEOUT_PATH_COLLISION")
    return {"lease": lease_pin, "completion": completion_pin}


def validate(spec: Any, thread: str) -> None:
    fields = {"task_id", "active_lease", "completion_path", "command", "expected_receipt"}
    if not isinstance(spec, dict) or set(spec) not in (fields, fields | {"mode"}):
        fail("CLOSEOUT_FIELDS_INVALID")
    mode = spec.get("mode", "source")
    if mode not in ("source", "external_deployment"):
        fail("CLOSEOUT_MODE_INVALID")
    if not isinstance(spec["task_id"], str) or not TASK_ID.fullmatch(spec["task_id"]):
        fail("CLOSEOUT_TASK_ID_INVALID")
    lease = spec["active_lease"]
    if not isinstance(lease, dict) or set(lease) != {"path", "sha256"} or not isinstance(lease["sha256"], str) or not SHA256.fullmatch(lease["sha256"]):
        fail("CLOSEOUT_LEASE_IDENTITY_INVALID")
    paths(spec)
    expected = spec["expected_receipt"]
    if (not isinstance(expected, dict) or not expected or expected.get("task_id") != spec["task_id"]
            or expected.get("thread_id") != thread or not isinstance(expected.get("finish_state"), str)
            or not expected["finish_state"].strip() or
            ("ok" in expected and expected["ok"] is not True)):
        fail("CLOSEOUT_EXPECTED_RECEIPT_INVALID")
    if mode == "external_deployment":
        base = expected.get("base_commit")
        if (expected["finish_state"] != "committed" or not isinstance(base, str)
                or not COMMIT.fullmatch(base) or expected.get("head_commit") != base
                or expected.get("committed_paths") != [] or expected.get("commit_attribution") != {}):
            fail("CLOSEOUT_EXTERNAL_SOURCE_IDENTITY_INVALID")
        command = spec.get("command")
        if (not isinstance(command, dict) or not isinstance(command.get("argv"), list)
                or any(isinstance(arg, str) and arg.split("=", 1)[0].startswith("--task-commit")
                       for arg in command["argv"])):
            fail("CLOSEOUT_EXTERNAL_TASK_COMMIT_FLAG")
    elif expected["finish_state"] == "committed":
        attribution = expected.get("commit_attribution")
        commits = attribution.get("task_commit_ids") if isinstance(attribution, dict) else None
        if not isinstance(commits, list) or not commits or any(not isinstance(sha, str) or not COMMIT.fullmatch(sha) for sha in commits):
            fail("CLOSEOUT_COMMITS_REQUIRED")
    # The approved plan's canonical digest binds this bounded JSON object.


def lease_identity(spec: dict[str, Any], pin: dict[str, Any], thread: str) -> None:
    path = Path(spec["active_lease"]["path"])
    if paths(spec) != pin:
        fail("CLOSEOUT_PARENT_RETARGETED")
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            fail("CLOSEOUT_LEASE_NOT_REGULAR")
        if caller._file_sha256(path) != spec["active_lease"]["sha256"]:
            fail("CLOSEOUT_LEASE_DRIFT")
        record = caller._load_json(path, limit=LIMIT, code="NOKIY_DEPLOYMENT_CLOSEOUT_LEASE_INVALID_JSON")
        if caller._file_sha256(path) != spec["active_lease"]["sha256"]:
            fail("CLOSEOUT_LEASE_DRIFT")
    except (FileNotFoundError, IsADirectoryError):
        fail("CLOSEOUT_LEASE_MISSING")
    if (not isinstance(record, dict) or record.get("task_id") != spec["task_id"]
            or record.get("thread_id") != thread):
        fail("CLOSEOUT_LEASE_OWNER_MISMATCH")
    if spec.get("mode", "source") == "external_deployment":
        expected = spec["expected_receipt"]
        root = record.get("root")
        scopes = record.get("write_scopes")
        if (record.get("ok") is not True or record.get("violations") != []
                or record.get("base_commit") != expected["base_commit"]
                or not isinstance(root, str) or not Path(root).is_absolute()
                or ".." in Path(root).parts or any(ch in root for ch in "*?[]{}\0")
                or not isinstance(scopes, list) or not scopes):
            fail("CLOSEOUT_EXTERNAL_LEASE_INVALID")
        try:
            canonical_root = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            fail("CLOSEOUT_EXTERNAL_LEASE_INVALID")
        if not canonical_root.is_dir():
            fail("CLOSEOUT_EXTERNAL_LEASE_INVALID")
        for scope in scopes:
            if (not isinstance(scope, str) or not Path(scope).is_absolute()
                    or ".." in Path(scope).parts or any(ch in scope for ch in "*?[]{}\0")):
                fail("CLOSEOUT_EXTERNAL_SCOPE_INVALID")
            try:
                canonical_scope = Path(scope).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                fail("CLOSEOUT_EXTERNAL_SCOPE_INVALID")
            if (canonical_scope == canonical_root or canonical_root in canonical_scope.parents
                    or canonical_scope in canonical_root.parents):
                fail("CLOSEOUT_EXTERNAL_SCOPE_INVALID")


def subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    return type(expected) is type(actual) and expected == actual


def receipt(spec: dict[str, Any], pin: dict[str, Any], thread: str) -> str:
    if paths(spec) != pin:
        fail("CLOSEOUT_PARENT_RETARGETED")
    lease = Path(spec["active_lease"]["path"])
    # lstat notices dangling leaf symlinks, which exists()/is_file() would conceal.
    try:
        lease.lstat()
    except FileNotFoundError:
        pass
    else:
        fail("CLOSEOUT_LEASE_STILL_ACTIVE")
    path = Path(spec["completion_path"])
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            fail("CLOSEOUT_RECEIPT_NOT_REGULAR")
        digest = caller._file_sha256(path)
        record = caller._load_json(path, limit=LIMIT, code="NOKIY_DEPLOYMENT_CLOSEOUT_RECEIPT_INVALID_JSON")
        if caller._file_sha256(path) != digest:
            fail("CLOSEOUT_RECEIPT_DRIFT")
    except FileNotFoundError:
        fail("CLOSEOUT_RECEIPT_MISSING")
    expected = spec["expected_receipt"]
    if (not isinstance(record, dict) or record.get("ok") is not True
            or not subset(expected, record) or record.get("task_id") != spec["task_id"]
            or record.get("thread_id") != thread or record.get("finish_state") != expected["finish_state"]):
        fail("CLOSEOUT_RECEIPT_MISMATCH")
    if spec.get("mode", "source") == "external_deployment":
        # An empty dict is vacuously a subset of any dict; enforce *exactly* no
        # task commits and no source paths, including any optional native fields.
        if (record.get("commit_attribution") != {} or record.get("committed_paths") != []
                or ("dirty_task_paths" in record and record["dirty_task_paths"] != [])
                or any(key in record and record[key] != [] for key in (
                    "task_owned_dirty_paths", "task_owned_new_dirty_paths",
                    "committed_paths_outside_write_scopes"))
                or ("new_source_changes" in record and
                    not (type(record["new_source_changes"]) is list and record["new_source_changes"] == []
                         or record["new_source_changes"] is False))):
            fail("CLOSEOUT_RECEIPT_MISMATCH")
    if paths(spec) != pin:
        fail("CLOSEOUT_PARENT_RETARGETED")
    try:
        lease.lstat()
    except FileNotFoundError:
        return digest
    fail("CLOSEOUT_LEASE_STILL_ACTIVE")
